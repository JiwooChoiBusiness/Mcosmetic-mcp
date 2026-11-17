---
title: M Cosmetic Knowledge MCP
emoji: 🧴
colorFrom: pink
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
short_description: MCP server answering questions on the M Cosmetic dataset
---

# M Cosmetic Knowledge MCP

`ai365-Mcosmetic-knowledge-updated_2026-10-22.txt`(14,062행, 약 510KB) 전체를 AI가 검색·조회해서
**데이터셋에 있는 모든 내용에 근거를 달아 답하도록** 만든 MCP 서버입니다.
외부 AI(서버 URL 방식, Streamable HTTP)와 Claude Code(stdio) 둘 다 연결할 수 있습니다.

## 1. 구성

```
MCP_Cosmetic/
  data/ai365-Mcosmetic-knowledge-updated_2026-10-22.txt   원본 데이터 (복사본)
  cosmetic_kb/kb.py       파서 + 한국어 BM25 검색 + 구조화 레코드
  cosmetic_kb/gloss.py    영어 본문용 한국어 용어집(색인 시에만 사용)
  cosmetic_kb/server.py   MCP 서버 (도구 9개, stdio / HTTP)
  run_server.py           어느 폴더에서나 실행 가능한 런처
  eval/evaluate.py        자동 질문 생성 + recall 측정
  eval/llm_questions.jsonl  LLM이 따로 쓴 자연어 질문 220개
  tests/test_kb.py        단위 테스트 (pytest)
  tests/e2e_check.py      실제 MCP 클라이언트로 stdio/HTTP 전체 도구 검증
  Dockerfile              클라우드 배포용
  docs/MCP_SPEC.md        MCP 명세서 (도구·파라미터·반환값·접속 사양)
  docs/PROMPTS.md         예시 프롬프트 40개 + 심화 23개 (기대 답변 포함)
  .mcp.json               Claude Code 연결 설정 (git 제외)
```

데이터 구조: 섹션 0~15, 이메일 226건(E-xxxx), 업무 Case 63건(RFQ 37 / ORD 14 / INQ 12),
품목 12, 고객 12(연락처·서명 포함), Sales Order 27, 처리 오류 7, 알림, 데모 시나리오, 대시보드.

## 2. 도구 (외부 AI 화면의 "Tools" 표에 그대로 보입니다)

| 도구 | 용도 | 주요 파라미터 |
|---|---|---|
| `ask_knowledge` | **기본 도구.** 아무 질문이나 받아 근거 원문(섹션·행 번호)과 관련 레코드를 돌려줌 | question, top_k |
| `search_knowledge` | 키워드/자연어 검색 (섹션·종류 필터 가능) | query, top_k, section, kind |
| `get_record` | ID나 이름으로 레코드 전체 조회 (E-, RFQ/ORD/INQ-, SO-, MC-, CUST-, ERR-, T-, 고객명·품목명) | record_id |
| `find_emails` | 이메일 226건 조건 검색 + 개수·분포 (몇 건? 어떤 메일?) | customer, classification, direction, status, date_from/to, item, assignee … |
| `find_cases` | Case 63건 조건 검색 + 개수·분포 | customer, case_type, status, done, item, sales_order … |
| `lookup_master` | 마스터/참조 표 전체 또는 키워드 필터 | table, keyword |
| `grep_text` | 원문 전체에서 정확한 문구 찾기 (행 번호) | pattern, regex, max_hits |
| `read_lines` | 원문 행 범위 그대로 읽기 | start_line, end_line |
| `list_topics` | 목차·ID 형식·고객/품목/담당자 목록 | – |

서버 지침(instructions)과 각 도구 설명에 "반드시 근거를 인용하고, 없으면 '데이터셋에 없음'이라고 답하라"가 들어 있습니다.

## 3. 실행

```bash
cd "C:/Users/ElonChoi(최지우)/Desktop/MCP_Cosmetic"
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt -r requirements-dev.txt
```

- stdio (Claude Code): `.venv/Scripts/python run_server.py`
- HTTP (외부 AI): `.venv/Scripts/python run_server.py --http --port 8765` → `http://<호스트>:8765/mcp`
  - 상태 확인: `GET /health`
  - 인증(선택): 환경변수 `COSMETIC_MCP_TOKEN=<비밀값>`을 주면 `Authorization: Bearer <비밀값>` 또는 `X-API-Key: <비밀값>` 헤더가 있어야 접속됩니다. 공개 URL로 열 때는 꼭 설정하세요.
  - stateless + JSON 응답 모드라 세션 유지가 필요 없고, `/mcp/`(끝 슬래시)도 받습니다.

## 4. 연결 방법

### 외부 AI (스크린샷의 MCP 등록 화면)
| 항목 | 값 |
|---|---|
| 에이전트명 | 예: M코스메틱 지식 |
| 서버 URL | **`https://mcosmetic.vercel.app/mcp`** (현재 배포 주소) |
| 인증 | None (공개 데이터. 토큰을 설정했다면 Bearer = 그 토큰) |

> 주의: Vercel이 배포마다 만드는 긴 주소(`mcosmetic-xxxx-choi-902a.vercel.app`)는 Vercel 로그인 보호가 걸려 있어
> 외부 AI가 접속할 수 없습니다. 항상 프로젝트 기본 주소 `mcosmetic.vercel.app`을 쓰세요.
> 배포 확인: `python tests/e2e_check.py https://mcosmetic.vercel.app/mcp`
| 설명 | M코스메틱 이메일 에이전트 지식 데이터(2026-10-22 스냅샷) 검색·조회 |

외부 AI는 인터넷(클라우드)에서 접속하므로 **내 PC의 localhost 주소로는 연결되지 않습니다.** 다음 중 하나가 필요합니다.
0. **GitHub + Vercel (무료, 현재 선택한 방식):** Vercel 무료(Hobby) 요금제가 `app.py`의 `app`을 서버리스 함수로 실행합니다.
   1. GitHub에 **Public** 저장소를 만들고 이 폴더를 push
   2. https://vercel.com 에 GitHub 계정으로 가입 → **Add New → Project** → 그 저장소 **Import** → **Deploy**
      (설정은 기본값 그대로. `requirements.txt`, `.python-version`, `vercel.json`이 이미 들어 있음)
   3. URL: `https://<프로젝트이름>.vercel.app/mcp` (상태 확인: `/health`)
   - 이후 GitHub에 push할 때마다 자동으로 다시 배포됩니다.
   - 서버리스라 잠들지 않고, 오래 안 쓴 뒤 첫 요청만 1~3초 더 걸립니다.
   - (Hugging Face Spaces는 2026년 기준 Docker 방식이 유료로 바뀌어 제외)
1. **클라우드 배포 (유료, 주소 고정):** `Dockerfile`로 Azure Container Apps / App Service 등에 올리고 `COSMETIC_MCP_TOKEN` 설정 → `https://<앱주소>/mcp`
2. **터널 (빠른 테스트):** PC에서 서버를 켜고 `cloudflared tunnel --url http://localhost:8765` 같은 터널로 임시 https 주소 발급 → `https://xxxx.trycloudflare.com/mcp` (PC가 켜져 있어야 하고, 주소가 실행할 때마다 바뀜)
3. **사내망:** 외부 AI가 사내망에서 PC에 직접 닿는다면 `http://<PC IP>:8765/mcp` (Windows 방화벽에서 8765 포트 허용 필요)

### Claude Code
이 폴더(`MCP_Cosmetic`)에서 Claude Code를 열면 `.mcp.json`의 `mcosmetic-knowledge` 서버를 승인하라는 창이 뜹니다. 승인하면 끝.
(`.mcp.json`은 절대경로를 쓰므로 다른 폴더로 옮기면 경로를 고치세요.)

## 5. 검증 결과 (2026-11-17)

`python -m eval.evaluate` — 질문 6,709개, 검색 1.6ms/질문. "strict@k" = 정답이 있는 원문 행을 포함한 결과가 상위 k개 안에 있음.

| 질문 세트 | 개수 | @1 | @3 | @5 | @10 |
|---|---|---|---|---|---|
| LLM이 쓴 자연어 질문 – 직접형 | 111 | 92.8% | 100% | **100%** | 100% |
| LLM이 쓴 자연어 질문 – 바꿔 말하기(ID 없음) | 109 | 67.0% | 96.3% | **98.2%** | 99.1% |
| 템플릿 질문 – 이메일 식별/본문/담당자 | 678 | – | – | 94.7~99.6% | 100% |
| 템플릿 질문 – Case 상태/요청/액션 | 182 | – | – | 96.8~100% | 100% |
| 템플릿 질문 – 마스터(품목·고객·연락처·SO·오류) | 145 | 64.8% | 91.0% | **94.5%** | 100% |
| 원문의 고유한 모든 행을 그 내용으로 검색 | 2,742 | 66.2% | 90.2% | 94.1% | 97.8% |
| 위 행을 단어 40% 삭제·순서 섞어 검색(스트레스) | 2,742 | 55.7% | 80.9% | 86.3% | 92.1% |

- `ask_knowledge` 기본 top_k=6 이고, 개수·목록 질문은 `find_emails`/`find_cases`(전수 필터)가, 정확한 숫자·문구는 `grep_text`(전체 행 100% 도달)가 맡습니다.
- 집계 정합성: 수신 133 / 발신 93 / Case 63 → 대시보드의 누적 수치와 일치.
- `python tests/e2e_check.py`: stdio, HTTP, HTTP+토큰 세 방식에서 도구 9개 전부 실제 클라이언트로 호출 → **118개 검사 모두 통과** (토큰 없음/틀림 → 401 확인 포함).
- `python -m pytest tests`: 9개 통과 (모든 비어 있지 않은 행이 검색 단위에 포함됨을 검사).

## 6. 데이터 갱신
새 스냅샷 파일로 바꾸려면 `data/`의 파일을 교체하거나 환경변수 `COSMETIC_KB_FILE`로 경로를 지정한 뒤 서버를 재시작하세요. 형식(섹션 제목+밑줄, `EMAIL E-…`, `CASE …`)이 같으면 그대로 동작합니다. 바꾼 뒤 `python -m eval.evaluate`로 recall을 다시 확인하세요.
