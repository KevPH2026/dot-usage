"""适配器注册表。

想加一个新适配器，实现 `collect(spec) -> AdapterResult` 然后注册到这里，
配置里写 `adapter: 你的名字` 就能用。
"""

from __future__ import annotations

from typing import Callable

from ..base import AdapterResult, SourceSpec

_REGISTRY: dict[str, Callable[[SourceSpec], AdapterResult]] = {}


def register(name: str):
    """装饰器：把适配器注册进表。"""

    def deco(fn):
        _REGISTRY[name] = fn
        return fn

    return deco


def get(name: str):
    return _REGISTRY.get(name)


def names() -> list[str]:
    return sorted(_REGISTRY)


# 导入即注册。写在这里而不是用装饰器，是为了避免「包 __init__ 导入子模块、
# 子模块又 import 包 __init__」的循环导入。
from . import codex_rollouts as _codex  # noqa: E402,F401
from . import jsonl_usage as _jsonl  # noqa: E402,F401
from . import sqlite_usage as _sqlite  # noqa: E402,F401

_REGISTRY["sqlite_usage"] = _sqlite.collect
_REGISTRY["jsonl_usage"] = _jsonl.collect
_REGISTRY["codex_rollouts"] = _codex.collect
