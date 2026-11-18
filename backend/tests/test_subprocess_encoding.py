"""Issue #36: a subprocess call with `text=True` (or `universal_newlines=True`) and no `encoding=`
decodes the child's output with the locale's code page. Linux CI has UTF-8, so CI passes, but on a
Chinese Windows (cp936) UTF-8 output comes back garbled or raises. Ruff's PLW1514 does not cover
subprocess, so this test does: every such call under backend/ must name its encoding."""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOTS = ("app", "tests", "alembic", "scripts", "bench")
CALLS = {"run", "Popen", "check_output", "call", "check_call"}
TEXT_FLAGS = {"text", "universal_newlines"}


def _on(flag: ast.expr) -> bool:
    """`text=False` or `text=None` is off; anything else (True, 1, a variable) may be on."""
    return not (isinstance(flag, ast.Constant) and not flag.value)


def missing_encoding(source: str) -> list[int]:
    """Lines of subprocess calls that ask for text without naming an encoding."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            name = func.attr if func.value.id == "subprocess" else None
        else:
            name = func.id if isinstance(func, ast.Name) else None
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        text = any(_on(kw[f]) for f in TEXT_FLAGS if f in kw)
        if name in CALLS and text and "encoding" not in kw:
            lines.append(node.lineno)
    return lines


def test_the_check_finds_a_call_without_encoding() -> None:
    assert missing_encoding("import subprocess\nsubprocess.run(['node'], text=True)\n") == [2]
    popen = "from subprocess import Popen\nPopen(['x'], universal_newlines=1)"
    assert missing_encoding(popen) == [2]
    assert missing_encoding("subprocess.run(['x'], text=True, encoding='utf-8')") == []
    assert missing_encoding("subprocess.run(['x'], text=False)\nsubprocess.run(['x'])") == []


def test_every_subprocess_text_call_names_its_encoding() -> None:
    files = sorted(p for root in ROOTS for p in (BACKEND / root).rglob("*.py"))
    assert files, "no Python files found under backend/"
    bad = [f"{p.relative_to(BACKEND).as_posix()}:{line}" for p in files
           for line in missing_encoding(p.read_text(encoding="utf-8"))]
    assert bad == [], "add encoding=\"utf-8\" (issue #36): " + ", ".join(bad)
