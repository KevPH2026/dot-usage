"""输出目标（sink）调度。

核心只负责采集，送达哪儿是 sink 的事：

    run_sinks(rows, cfg, state)  →  按配置依次跑各 sink

**加一个新输出目标**（网页 / 终端 / Notion / 邮件）：
写一个 `Sink` 子类实现 `push()`，在模块里 `register()` 一下，
核心采集代码一行都不用动。

内置两个：
    local  写 state.json（唯一契约，桌面端读它）
    dot    推墨水屏设备
"""

from __future__ import annotations

from dotusage.util import config as cfgmod

from .base import (
    PushError,
    Sink,
    SinkResult,
    SinkSkip,
    all_sinks,
    get_sink,
    register,
    sink_status,
)

# 导入即注册（执行顺序由 base._ORDER 决定：local 先，dot 后）
from . import local  # noqa: F401,E402
from . import dot  # noqa: F401,E402

__all__ = [
    "PushError",
    "Sink",
    "SinkResult",
    "SinkSkip",
    "all_sinks",
    "get_sink",
    "register",
    "run_sinks",
    "sink_status",
]


def run_sinks(rows, cfg: dict, state: dict | None = None, overrides: dict | None = None,
              force: bool = False) -> list[SinkResult]:
    """按配置依次跑各 sink。

    - overrides: 临时开关覆盖（例如命令行 --no-dot）
    - force:     强制推送（跳过降频闸门）
    """
    results: list[SinkResult] = []
    overrides = overrides or {}
    for sink in all_sinks():
        name = sink.name
        try:
            enabled = sink.enabled(cfg)
        except Exception as e:  # noqa: BLE001
            results.append(SinkResult(name=name, ok=False, message=f"读取开关失败：{e}"))
            continue
        if name in overrides:
            enabled = bool(overrides[name])
        if not enabled:
            results.append(SinkResult(name=name, ok=True, skipped=True, message="已禁用"))
            continue
        try:
            msg = sink.push(rows, cfg, state or {}, force=force)
            results.append(SinkResult(name=name, ok=True, message=msg))
        except SinkSkip as e:
            results.append(SinkResult(name=name, ok=True, skipped=True, message=str(e)))
        except PushError as e:
            results.append(SinkResult(name=name, ok=False, message=str(e)))
        except Exception as e:  # noqa: BLE001
            cfgmod.log_line(f"sink {name} failed: {e}")
            results.append(SinkResult(name=name, ok=False, message=f"{type(e).__name__}: {e}"))
    return results
