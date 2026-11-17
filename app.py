"""ASGI entrypoint for serverless hosting (Vercel detects `app` in app.py).

MCP endpoint: https://<deployment>/mcp   (health check: /health)
Optional env COSMETIC_MCP_TOKEN -> requires "Authorization: Bearer <token>".
Local equivalent: python run_server.py --http
"""
import os

from cosmetic_kb.server import build_asgi_app

app = build_asgi_app(os.environ.get("COSMETIC_MCP_TOKEN") or None, "/mcp")
