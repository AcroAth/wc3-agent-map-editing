"""Every WC3_* setting the server reads must be passed through by .mcp.json.

Codex starts a stdio MCP server with only its core Windows variables plus the
names in ``env_vars``, so a setting missing there is silently ignored.
Pure Python, runs anywhere:  python tests/test_env_vars.py
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    used: set[str] = set()
    for path in (ROOT / "src").rglob("*.py"):
        used |= set(re.findall(r"""environ(?:\.get\(|\[)\s*["'](WC3_[A-Z0-9_]+)""", path.read_text(encoding="utf-8")))
    config = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    listed = set(config["mcpServers"]["wc3"]["env_vars"])
    missing = sorted(used - listed)
    ok = bool(used) and not missing
    print(f"[{'PASS' if ok else 'FAIL'}] env_vars lists every WC3_* setting read in src: "
          f"{'missing ' + ', '.join(missing) if missing else sorted(used)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
