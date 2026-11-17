# Remote (Streamable HTTP) deployment: endpoint = https://<host>/mcp
# Works as-is on Hugging Face Spaces (Docker SDK, app_port 7860), Render, Azure, etc.
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir mcp==2.3.0
COPY cosmetic_kb ./cosmetic_kb
COPY data ./data
COPY run_server.py .
ENV PYTHONIOENCODING=utf-8 PORT=7860
EXPOSE 7860
# Optional: set COSMETIC_MCP_TOKEN to require "Authorization: Bearer <token>"
CMD ["sh", "-c", "python run_server.py --http --host 0.0.0.0 --port ${PORT}"]
