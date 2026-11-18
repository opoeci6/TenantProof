"""Runs a script under node and returns what it prints, parsed as JSON. Used by the tests that
check pure functions from web/index.html.

node writes UTF-8 whatever the locale. Without `encoding="utf-8"`, `text=True` decodes with the
locale's code page, so on a Chinese Windows (cp936) "·" comes back as "路" and "…" raises
UnicodeDecodeError (issue #36)."""

import json
import subprocess
from typing import Any


def node_json(script: str) -> Any:
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, encoding="utf-8", check=True
    )
    return json.loads(out.stdout)
