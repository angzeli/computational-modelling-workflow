"""Executable Python fixtures pinned to the interpreter running the tests."""
from __future__ import annotations

import ast
from pathlib import Path
import shlex
import re
import sys


def python_source(source: str, *, executable: str | None = None) -> str:
    """Keep the script in one file, including its adjacent-resource identity.

    A shell/Python preamble supports interpreter paths containing spaces without
    resolving the virtual-environment executable to its base interpreter.
    """
    lines = source.splitlines(keepends=True)
    if lines and lines[0].startswith("#!"):
        lines[0] = "\n"
    future_imports = []
    for node in ast.parse("".join(lines)).body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            future_imports.extend(lines[node.lineno - 1:node.end_lineno])
            lines[node.lineno - 1:node.end_lineno] = ["\n"] * (node.end_lineno - node.lineno + 1)
    interpreter = shlex.quote(executable or sys.executable)
    return (f"#!/bin/sh\n'''exec' {interpreter} \"$0\" \"$@\"\n' '''\n"
            + "".join(future_imports) + "".join(lines))


def write_python_script(path: Path, source: str, *, executable: str | None = None) -> Path:
    path.write_text(python_source(source, executable=executable), encoding="utf-8")
    path.chmod(0o700)
    return path


def vasp_environment(*, executable=None):
    """Select Python using the historical checker's non-expanding literal form."""
    python = sys.executable if executable is None else executable
    if re.fullmatch(r'/[A-Za-z0-9_./: +\-]+', python) is None:
        raise ValueError('VASP fixture interpreter must fit the static literal path contract')
    return ('PORT_ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)\n'
            f'CMW_MANAGED_PYTHON="{python}"\n')
