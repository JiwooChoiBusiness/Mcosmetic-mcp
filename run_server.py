"""Launcher usable from any working directory (Claude Code .mcp.json, schedulers, etc.).

  python run_server.py                 -> stdio
  python run_server.py --http          -> http://0.0.0.0:8765/mcp
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cosmetic_kb.server import main  # noqa: E402

if __name__ == "__main__":
    main()
