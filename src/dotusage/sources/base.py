"""数据源（source）插件机制。

**为什么要插件化**：dot-usage 一开始只有作者本机的几个工具写死在里面，
别人拿去用就得改 Python 代码。现在每个工具都是一个「源」，
用 YAML 描述清楚**去哪儿读、读什么字段、算它属于哪个平台**，
就能加自己的工具，不用碰核心代码。

一个源的描述长这样：

    - id: mytool                    # 唯一 id
      name: MyTool                  # 显示名
      platform: 自家                 # 归到哪个平台（卡片按平台合并）
      icon: chatgpt                 # 卡片图标（icons/png/<icon>.png）
      adapter: sqlite_usage         # 用哪个适配器读
      official: ""                  # 可选：走哪个官方配额 provider
      enabled: true
      options: {...}                # 适配器自己的参数

适配器有两类：

    builtin        内置采集器（作者本机那几个工具，走优化过的专用逻辑）
    sqlite_usage   通用 SQLite —— 大多数 CLI 工具的用量都在 SQLite 里
    jsonl_usage    通用 JSONL —— 每行一个 JSON 的会话日志

「通用适配器」是给别人的主要入口：只要你的工具把用量记在
某个 SQLite 表里，写一条 SQL + 一份字段映射就能接进来。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.schema import SourceReport, UsageRow, now_ts


@dataclass
class SourceSpec:
    """一个数据源的声明。"""

    id: str
    name: str = ""
    platform: str = ""
    icon: str = ""
    adapter: str = "builtin"
    builtin: str = ""
    official: str = ""
    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "SourceSpec":
        return cls(
            id=str(d.get("id") or "").strip(),
            name=str(d.get("name") or "").strip(),
            platform=str(d.get("platform") or "").strip(),
            icon=str(d.get("icon") or "").strip(),
            adapter=str(d.get("adapter") or "builtin").strip(),
            builtin=str(d.get("builtin") or "").strip(),
            official=str(d.get("official") or "").strip(),
            enabled=bool(d.get("enabled", True)),
            options=dict(d.get("options") or {}),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "platform": self.platform,
            "icon": self.icon, "adapter": self.adapter, "builtin": self.builtin,
            "official": self.official, "enabled": self.enabled, "options": self.options,
        }


@dataclass
class AdapterResult:
    rows: list[UsageRow] = field(default_factory=list)
    report: SourceReport | None = None


def expand(path: str) -> Path:
    """展开 ~ 和环境变量。"""
    return Path(os.path.expandvars(os.path.expanduser(str(path))))


def spec_platform(spec: SourceSpec) -> str:
    """源归属的平台名（卡片按它合并）。"""
    return spec.platform or spec.name or spec.id


def spec_label(spec: SourceSpec, model: str) -> str:
    """行标题：平台 + 模型名。"""
    base = spec_platform(spec)
    return f"{base} · {model}" if model else base


def row_from_fields(spec: SourceSpec, model: str, f: dict) -> UsageRow:
    """把适配器抽出来的字段拼成一行 UsageRow。

    字段名固定：input / output / reasoning / cache_read / cache_write / total / calls / ts / cost
    """
    def num(k: str) -> int:
        try:
            return int(float(f.get(k) or 0))
        except (TypeError, ValueError):
            return 0

    total = num("total")
    if not total:
        total = num("input") + num("output") + num("cache_read") + num("cache_write")
    ts = f.get("ts")
    return UsageRow(
        source=spec.id,
        label=spec_label(spec, model),
        model=model,
        kind="consumption",
        input_tokens=num("input"),
        output_tokens=num("output"),
        reasoning_tokens=num("reasoning"),
        cache_read_tokens=num("cache_read"),
        cache_write_tokens=num("cache_write"),
        total_tokens=total,
        calls=num("calls") or 1,
        cost_usd=float(f.get("cost") or 0) or None,
        updated_at=float(ts or 0) or now_ts(),
        path=str(spec.options.get("db") or spec.options.get("glob") or ""),
    )


def report_for(spec: SourceSpec, rows: list[UsageRow], error: str = "") -> SourceReport:
    rep = SourceReport(spec.id)
    rep.rows = len(rows)
    if error:
        rep.ok = False
        rep.error = error
    elif not rows:
        rep.note = "没有数据"
    return rep
