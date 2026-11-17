"""Verify every demo prompt in eval/demo_prompts.json through the MCP tools.

  python eval/check_demo_prompts.py                                  # in-process
  python eval/check_demo_prompts.py https://mcosmetic.vercel.app/mcp # over the public URL
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

prompts = json.loads((ROOT / "eval" / "demo_prompts.json").read_text(encoding="utf-8"))
URL = sys.argv[1] if len(sys.argv) > 1 else None

if URL:
    import httpx2

    def call(name, args):
        r = httpx2.post(URL, headers={"Accept": "application/json, text/event-stream"}, timeout=60,
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": args}}).json()["result"]
        return r.get("isError", False), r["content"][0]["text"]
else:
    from cosmetic_kb import server as s

    def call(name, args):
        try:
            return False, json.dumps(getattr(s, name)(**args), ensure_ascii=False)
        except Exception as ex:  # ToolError etc.
            return True, str(ex)

ok = 0
for i, p in enumerate(prompts, 1):
    c = p["check"]
    if c is None:
        print(f"{i:2d} SKIP (거절 확인용) {p['prompt']}")
        ok += 1
        continue
    if "ask" in c:
        err, txt = call("ask_knowledge", {"question": p["prompt"]})
        good = not err and c["ask"].replace(",", "") in txt.replace(",", "")
    else:
        err, txt = call(c["call"], c["args"])
        good = not err
        d = json.loads(txt) if not err else {}
        if "contains" in c:
            good = good and c["contains"] in txt
        if "total" in c:
            good = good and d.get("total", d.get("total_matching_lines")) == c["total"]
        if "min_total" in c:
            good = good and d.get("total", d.get("total_matching_lines", 0)) >= c["min_total"]
        if "count" in c:
            good = good and d.get("count", len(d.get("rows", []))) == c["count"]
    ok += good
    print(f"{i:2d} {'PASS' if good else 'FAIL'} {p['prompt']}")
print(f"\n{ok}/{len(prompts)} passed")
