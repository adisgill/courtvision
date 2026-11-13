"""`courtvision build` must never fetch from the network sources that need consent."""

from __future__ import annotations

import ast
import inspect

from courtvision import cli


def test_build_passes_every_option_explicitly():
    # build calls other commands as plain functions; an option it leaves out arrives as a
    # typer OptionInfo, which is truthy, so a left-out --fetch-current would fetch
    tree = ast.parse(inspect.getsource(cli))
    funcs = {f.name: f for f in tree.body if isinstance(f, ast.FunctionDef)}
    build = funcs["build"]
    checked = 0
    for call in ast.walk(build):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)):
            continue
        target = funcs.get(call.func.id)
        if target is None:
            continue
        params = [a.arg for a in target.args.args]
        passed = {k.arg for k in call.keywords} | set(params[: len(call.args)])
        assert set(params) <= passed, f"build calls {call.func.id} without {set(params) - passed}"
        for k in call.keywords:
            if k.arg == "fetch_current":
                assert isinstance(k.value, ast.Constant) and k.value.value is False
                checked += 1
    assert checked == 2  # salaries and coaches
    fetches = [
        c
        for c in ast.walk(build)
        if isinstance(c, ast.Call)
        and isinstance(c.func, ast.Name)
        and c.func.id == "ingest_injuries"
    ]
    assert fetches and all(any(k.arg == "fetch" and isinstance(k.value, ast.Constant)
                               and k.value.value is False for k in c.keywords)
                           for c in fetches)  # fmt: skip
