"""End-to-end check over real MCP transports.

  python tests/e2e_check.py            # stdio + HTTP (no auth) + HTTP (token auth)
Each tool is called through an actual MCP client session and its result is asserted.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx2
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
EXPECTED_TOOLS = {"ask_knowledge", "search_knowledge", "get_record", "find_emails", "find_cases",
                  "lookup_master", "grep_text", "read_lines", "list_topics",
                  "erp_price_lookup", "erp_inventory_lookup", "erp_shipment_lookup", "erp_sales_order_lookup",
                  "erp_register_order", "send_email"}


def payload(res) -> dict:
    sc = getattr(res, "structured_content", None)
    if sc:
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(res.content[0].text)


async def exercise(session: ClientSession, label: str) -> list[str]:
    log = []

    def ok(name, cond, info=""):
        log.append(f"[{label}] {'PASS' if cond else 'FAIL'} {name} {info}")
        if not cond:
            raise AssertionError(f"{label}: {name} failed {info}")

    init = session.initialize_result
    ok("initialize", init is not None and "M코스메틱" in (init.instructions or ""), init.server_info.name if init else "")
    tools = {t.name: t for t in (await session.list_tools()).tools}
    ok("list_tools", EXPECTED_TOOLS <= set(tools), f"{len(tools)} tools")

    async def call(name, args):
        r = await session.call_tool(name, args)
        ok(f"{name} not error", not r.is_error, (r.content[0].text[:120] if r.is_error else ""))
        return payload(r)

    r = await call("ask_knowledge", {"question": "누리뷰티가 카밍 세럼 300개 견적 문의한 메일의 납기 요청일은?"})
    ok("ask_knowledge top hit E-1001", r["evidence"][0]["record_id"] in ("E-1001", "RFQ-2026-00123"),
       r["evidence"][0]["source"])
    ok("ask_knowledge cites lines", all("line_start" in h for h in r["evidence"]))

    r = await call("search_knowledge", {"query": "SO-20261018-00109 송장번호", "top_k": 3})
    ok("search_knowledge SO row", any("CJ-6120-4458-9921" in h["text"] for h in r["results"]))

    r = await call("get_record", {"record_id": "E-1001"})
    ok("get_record email", r["fields"]["Subject"] == "카밍 세럼 견적 문의드립니다" and
       r["extracted_entities"]["deliveryDate"]["value"] == "2026-11-16")
    r = await call("get_record", {"record_id": "RFQ-2026-00118"})
    ok("get_record case", r["fields"]["entity"]["unitPrice"] == 11410 and len(r["timeline"]) == 3)
    r = await call("get_record", {"record_id": "온유글로벌 33"})
    ok("get_record customer by name", r["contact"]["n"] == "Sarah Kim")
    r = await call("get_record", {"record_id": "MC-CIC-SR050"})
    ok("get_record item", r["data"]["Stock"] == 3100)

    r = await call("find_emails", {})
    ok("find_emails all = 324", r["total"] == 324)
    r = await call("find_emails", {"direction": "out"})
    ok("find_emails out = 140 (dashboard 'Cumulative outgoing emails')", r["total"] == 140, str(r["total"]))
    r = await call("find_emails", {"direction": "in"})
    ok("find_emails in = 184 (dashboard 'Cumulative incoming')", r["total"] == 184, str(r["total"]))
    r = await call("find_emails", {"customer": "누리뷰티", "classification": "견적"})
    ok("find_emails filter", r["total"] >= 1 and all(e["customer"] == "누리뷰티 02" for e in r["emails"]))

    r = await call("find_cases", {})
    ok("find_cases all = 78", r["total"] == 78)
    r = await call("find_cases", {"case_type": "ORD"})
    ok("find_cases ORD = 18", r["total"] == 18, str(r["total"]))

    r = await call("lookup_master", {"table": "items"})
    ok("lookup_master items = 12", r["count"] == 12)
    r = await call("lookup_master", {"table": "dashboard", "keyword": "Total work cases"})
    ok("lookup_master dashboard", any("78" in x["text"] for x in r["rows"]))

    r = await call("grep_text", {"pattern": "11,410"})
    ok("grep_text", r["total_matching_lines"] >= 1 and all("11,410" in h["text"] for h in r["hits"]))

    r = await call("read_lines", {"start_line": 1, "end_line": 4})
    ok("read_lines", "Knowledge Base" in r["text"] and r["text"].startswith("1: "))

    r = await call("list_topics", {})
    ok("list_topics", len(r["sections"]) == 16 and r["total_lines"] == 19638 and r["snapshot_date"] == "2026-11-25")

    # ---- simulated ERP / mail actions
    ok("write tools not read-only", tools["erp_register_order"].annotations.read_only_hint is False
       and tools["erp_price_lookup"].annotations.read_only_hint is True)
    r = await call("erp_price_lookup", {"item": "글로우 크림 50g", "quantity": 500, "customer": "다온코스메틱"})
    ok("erp_price_lookup 11,410", r["erpResult"]["price"] == 11410 and "simulated" not in r)
    r = await call("erp_inventory_lookup", {"item": "MC-CAL-CR050", "quantity": 500})
    ok("erp_inventory_lookup allocated", r["erpResult"]["allocated"] == 1000 and r["erpResult"]["enough"] is True)
    r = await call("erp_shipment_lookup", {"sales_order": "SO-20261018-00109"})
    ok("erp_shipment_lookup tracking", r["erpResult"]["tracking"] == "CJ-6120-4458-9921")
    r = await call("erp_sales_order_lookup", {"sales_order": "SO-20260929-00063"})
    ok("erp_sales_order_lookup total", r["erpResult"]["total"] == 10366400)
    r = await call("erp_register_order", {"case_id": "ORD-2026-00143"})
    ok("register blocked (no delivery date)", r["status"] == "blocked" and r["missing_fields"] == ["deliveryDate"])
    p = await call("erp_register_order", {"case_id": "ORD-2026-00146"})
    ok("register preview", p["status"] == "preview" and "SO-20261125-00146" in p["summary_text"])
    nope = await session.call_tool("erp_register_order", {"case_id": "ORD-2026-00146", "confirm": True})
    ok("confirm without preview_id -> error", nope.is_error)
    d = await call("erp_register_order", {"case_id": "ORD-2026-00146", "confirm": True, "preview_id": p["preview_id"]})
    ok("register done", d["status"] == "done" and d["orderResult"]["so"] == "SO-20261125-00146")
    mail = {"to": "sarah.kim@onyuglobal.com", "subject": "주문 접수 확인", "body": "주문 접수되었습니다.\nM코스메틱 이수진 드림",
            "case_id": "ORD-2026-00124"}
    p = await call("send_email", mail)
    d = await call("send_email", {**mail, "confirm": True, "preview_id": p["preview_id"]})
    ok("send_email preview -> sent", p["status"] == "preview" and d["mail"] == "sent" and d["messageId"].startswith("MSG-"))

    bad = await session.call_tool("get_record", {"record_id": "E-9999"})
    ok("unknown id -> is_error with message", bad.is_error and "찾지 못했습니다" in bad.content[0].text)
    return log


async def run_stdio() -> list[str]:
    params = StdioServerParameters(command=PY, args=["-m", "cosmetic_kb.server"], cwd=str(ROOT),
                                   env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await exercise(s, "stdio")


def free_port() -> int:
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


async def run_http(token: str | None) -> list[str]:
    port = free_port()
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop("COSMETIC_MCP_TOKEN", None)
    if token:
        env["COSMETIC_MCP_TOKEN"] = token
    proc = subprocess.Popen([PY, "-m", "cosmetic_kb.server", "--http", "--host", "127.0.0.1", "--port", str(port)],
                            cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    label = f"http{'+token' if token else ''}"
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if httpx2.get(base + "/health", timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.2)
        log = [f"[{label}] PASS health {httpx2.get(base + '/health').json()}"]
        if token:
            r = httpx2.post(base + "/mcp", json={}, headers={"Accept": "application/json, text/event-stream"})
            assert r.status_code == 401, r.status_code
            log.append(f"[{label}] PASS no token -> 401")
            r = httpx2.post(base + "/mcp", json={}, headers={"Authorization": "Bearer wrong"})
            assert r.status_code == 401
            log.append(f"[{label}] PASS wrong token -> 401")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with httpx2.AsyncClient(headers=headers, timeout=60) as hc:
            async with streamable_http_client(base + "/mcp", http_client=hc) as streams:
                r, w = streams[0], streams[1]
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    log += await exercise(s, label)
        return log
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def run_remote(url: str) -> list[str]:
    """Check an already deployed server, e.g. https://mcosmetic.vercel.app/mcp"""
    base = url.rstrip("/").removesuffix("/mcp")
    log = [f"[remote] PASS health {httpx2.get(base + '/health', timeout=60).json()}"]
    async with httpx2.AsyncClient(timeout=60) as hc:
        async with streamable_http_client(base + "/mcp", http_client=hc) as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                await s.initialize()
                log += await exercise(s, "remote")
    return log


async def main() -> None:
    results = []
    if len(sys.argv) > 1:  # python tests/e2e_check.py https://<host>/mcp
        results += await run_remote(sys.argv[1])
        print("\n".join(results))
        print(f"\nTOTAL {len(results)} checks, {sum('FAIL' in x for x in results)} failed")
        return
    results += await run_stdio()
    results += await run_http(None)
    results += await run_http("test-token-123")
    print("\n".join(results))
    n_fail = sum("FAIL" in x for x in results)
    print(f"\nTOTAL {len(results)} checks, {n_fail} failed")


if __name__ == "__main__":
    asyncio.run(main())
