"""Sink 概念：采集结果 → 输出目标。

**为什么要有这一层**：核心只负责「把用量采出来」，送达哪儿是 sink 的事。
加一个新输出目标（网页 / 终端 / Notion / 邮件）只要写一个 Sink 子类并注册，
核心代码一行都不用改。

一个 sink 就是一个实现了三件事的对象：
    name     唯一名字（配置里用它开关）
    enabled  读配置决定开不开
    push     真正干活，返回一句给人看的话
"""

from __future__ import annotations

from dataclasses import dataclass, field


class PushError(RuntimeError):
    """推送失败（网络、鉴权、服务端拒绝……）。"""


class SinkSkip(Exception):
    """这个 sink 这次不推（比如还没配密钥）——不是故障，是正常跳过。"""


@dataclass
class SinkResult:
    """一个 sink 跑完之后的结论。"""

    name: str
    ok: bool = True
    skipped: bool = False
    message: str = ""
    detail: dict = field(default_factory=dict)


class Sink:
    """一个输出目标的基类。"""

    name: str = ""
    label: str = ""
    #: 是否需要 API 密钥之类的外部凭据（doctor 里会提示）
    needs_secret: bool = False

    def enabled(self, cfg: dict) -> bool:
        """开不开。默认读 sources.toml 的 [sink.<name>] enabled。"""
        from dotusage.util import config as cfgmod

        return cfgmod.sink_enabled(self.name, default=True)

    def push(self, rows, cfg: dict, state: dict, force: bool = False) -> str:
        """把采集结果送到这个 sink。返回一句给人看的总结。"""
        raise NotImplementedError


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------
_REGISTRY: dict[str, Sink] = {}
#: sink 的执行顺序（前面的先跑）。local 必须最先——它写 state.json，
#: 后面的 sink 和桌面端都靠这个文件。
_ORDER: list[str] = ["local", "dot"]


def register(sink: Sink) -> Sink:
    """注册一个 sink（重复注册会覆盖旧的）。"""
    if not sink.name:
        raise ValueError("sink 必须有 name")
    _REGISTRY[sink.name] = sink
    return sink


def get_sink(name: str) -> Sink | None:
    return _REGISTRY.get(name)


def all_sinks() -> list[Sink]:
    """按固定顺序返回所有已注册的 sink。"""
    names = [n for n in _ORDER if n in _REGISTRY]
    names += [n for n in _REGISTRY if n not in _ORDER]
    return [_REGISTRY[n] for n in names]


def sink_status(cfg: dict) -> list[dict]:
    """每个 sink 的开关状态（给 `dotusage sinks` 用）。"""
    out: list[dict] = []
    for s in all_sinks():
        row = {"name": s.name, "label": s.label or s.name, "enabled": False, "error": ""}
        try:
            row["enabled"] = bool(s.enabled(cfg))
        except Exception as e:  # noqa: BLE001
            row["error"] = str(e)[:120]
        out.append(row)
    return out
