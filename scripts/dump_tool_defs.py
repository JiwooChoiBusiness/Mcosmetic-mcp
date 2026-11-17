"""Write docs/TOOLS_FOR_AI.md: exactly what an MCP client receives from this server
(initialize instructions + tools/list), i.e. what the AI model reads before choosing a tool.

  python scripts/dump_tool_defs.py                                  # from local code
  python scripts/dump_tool_defs.py https://mcosmetic.vercel.app/mcp # from the deployed server
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if len(sys.argv) > 1:
    import httpx2

    def rpc(method, params=None):
        r = httpx2.post(sys.argv[1], headers={"Accept": "application/json, text/event-stream"}, timeout=60,
                        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
        return r.json()["result"]

    init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "dump", "version": "1"}})
    tools = rpc("tools/list")["tools"]
    instructions = init.get("instructions", "")
else:
    import asyncio

    from cosmetic_kb.server import INSTRUCTIONS, mcp
    instructions = INSTRUCTIONS
    tools = [t.model_dump(by_alias=True, exclude_none=True) for t in asyncio.run(mcp.list_tools())]


def typ(s):
    if "type" in s:
        return s["type"]
    return " 또는 ".join(a.get("type", "?") for a in s.get("anyOf", []))


out = ["# AI가 읽는 도구 정의 (MCP 서버가 실제로 보내는 내용)", "",
       "MCP 클라이언트(외부 AI 플랫폼, Claude 등)는 연결할 때 아래 두 가지를 받아 AI 모델에게 넘깁니다.",
       "AI는 **도구 이름 + 설명 + 파라미터 설명**을 읽고 어떤 도구를 어떤 값으로 부를지 정합니다.", "",
       "1. `initialize` 응답의 **서버 지침(instructions)** — 클라이언트가 시스템 프롬프트에 넣어 주는 경우가 많지만, 무시하는 플랫폼도 있음",
       "2. `tools/list` 응답의 **도구 목록** — 이름, 제목, 설명, 입력 스키마(파라미터별 타입·설명·기본값·범위), annotations(클라이언트용 표시)",
       "", "도구를 부른 뒤에는 도구 **응답 내용(JSON)**을 읽고 답을 만듭니다.", "",
       "> 이 파일은 `python scripts/dump_tool_defs.py`로 자동 생성됩니다. 문구를 바꾸려면 `cosmetic_kb/server.py`의 지침·docstring·Field description을 고치세요.",
       "", "## 1. 서버 지침 (instructions)", "", "```", instructions.strip(), "```", "",
       f"## 2. 도구 목록 ({len(tools)}개)", ""]
for t in tools:
    ann = t.get("annotations") or {}
    ro = ann.get("readOnlyHint")
    out += [f"### `{t['name']}`" + (f" — {t['title']}" if t.get("title") else ""), "",
            f"- 데이터 변경 여부(annotations): {'조회 전용 (readOnlyHint: true)' if ro else '데이터 변경 도구 (readOnlyHint: false) — 클라이언트가 실행 전 승인 창을 띄울 수 있음'}",
            "", "**설명 (AI가 읽는 글)**", "", "```", (t.get("description") or "").strip(), "```", ""]
    props = t.get("inputSchema", {}).get("properties", {})
    req = set(t.get("inputSchema", {}).get("required", []))
    if props:
        out += ["| 파라미터 | 타입 | 필수 | 기본값 | 설명 (AI가 읽는 글) |", "|---|---|---|---|---|"]
        for name, s in props.items():
            rng = " ".join(f"{k}={s[k]}" for k in ("minimum", "maximum") if k in s)
            d = (s.get("description", "") + (f" ({rng})" if rng else "")).replace("|", "\\|").replace("\n", " ")
            default = json.dumps(s["default"], ensure_ascii=False) if "default" in s else ""
            out.append(f"| `{name}` | {typ(s)} | {'✔' if name in req else ''} | {default} | {d} |")
        out.append("")
    else:
        out += ["파라미터 없음", ""]
(ROOT / "docs" / "TOOLS_FOR_AI.md").write_text("\n".join(out), encoding="utf-8")
print(f"wrote docs/TOOLS_FOR_AI.md ({len(tools)} tools)")
