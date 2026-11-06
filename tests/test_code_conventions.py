"""Guards for the code conventions of CLAUDE.md that a reader would otherwise have to remember."""
import ast
import glob
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON_DIRS = ("server", "utils")
NARRATION = re.compile(
    r"\b(no longer|used to|regression|previously|was fixed|bug fix|runbook|post-?mortem|incident)\b", re.I
)


def _python_files():
    for d in PYTHON_DIRS:
        yield from sorted(glob.glob(os.path.join(ROOT, d, "**", "*.py"), recursive=True))


def _parse(path):
    text = open(path, encoding="utf-8").read()
    # Databricks notebooks carry %magic lines that are not Python.
    return text, ast.parse("\n".join("pass" if line.startswith("%") else line for line in text.split("\n")))


def _rel(path, node):
    return f"{os.path.relpath(path, ROOT)}:{node.lineno}"


def _functions():
    for path in _python_files():
        _, tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield path, node


def test_no_print_calls():
    offenders = []
    for path in _python_files():
        _, tree = _parse(path)
        offenders += [
            _rel(path, n) for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"
        ]
    assert not offenders, offenders


def test_every_function_has_a_docstring_and_a_return_annotation():
    missing = [_rel(p, n) for p, n in _functions() if not ast.get_docstring(n) or n.returns is None]
    assert not missing, missing


def test_every_parameter_is_documented():
    offenders = []
    for path, node in _functions():
        doc = ast.get_docstring(node) or ""
        args = node.args
        names = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
        names += [a.arg for a in (args.vararg, args.kwarg) if a]
        for name in names:
            if name not in ("self", "cls") and f":param {name}:" not in doc and f":param *{name}:" not in doc:
                offenders.append(f"{_rel(path, node)} {name}")
    assert not offenders, offenders


def test_no_dbtitle_markers():
    offenders = [
        os.path.relpath(p, ROOT) for p in _python_files() if re.search(r"^# DBTITLE", open(p, encoding="utf-8").read(), re.M)
    ]
    assert not offenders, offenders


def test_no_commented_out_imports_or_prints():
    pattern = re.compile(r"^\s*#\s*(import \w|from \w+ import |print\()")
    offenders = []
    for path in _python_files():
        for i, line in enumerate(open(path, encoding="utf-8"), 1):
            if pattern.match(line):
                offenders.append(f"{os.path.relpath(path, ROOT)}:{i}")
    assert not offenders, offenders


def test_comments_do_not_narrate_history():
    offenders = []
    for path in _python_files():
        for i, line in enumerate(open(path, encoding="utf-8"), 1):
            stripped = line.strip()
            if stripped.startswith("#") and NARRATION.search(stripped):
                offenders.append(f"{os.path.relpath(path, ROOT)}:{i}")
    assert not offenders, offenders


def test_route_modules_stay_small():
    for path in glob.glob(os.path.join(ROOT, "server", "routers", "**", "*.py"), recursive=True):
        with open(path, encoding="utf-8") as f:
            assert sum(1 for _ in f) <= 600, os.path.relpath(path, ROOT)
