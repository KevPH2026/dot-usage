"""Codex 适配器（专用）。

Codex 的会话日志是 JSONL，但每个文件动辄几百 MB，
且 token_count 事件只在文件尾部追加 —— 全量扫会卡死。
所以这里不是通用 JSONL 适配器能做的事，单独写一个。

读法：
    - 尾部扫描拿最近 24h 的增量（last_token_usage 之和，不是 total_token_usage）
    - 顺带记下每个文件的工作目录（cwd），供「项目烧钱榜」用
"""

from __future__ import annotations

from ...core.schema import now_ts
from ..base import AdapterResult, SourceSpec, report_for

DAY = 86400.0


def collect(spec: SourceSpec) -> AdapterResult:
    from ..builtin import collect_codex

    rows, rep = collect_codex()
    # builtin 采集器用固定 source 名，这里按配置改写，让卡片能按平台归并
    out = []
    for r in rows:
        r.source = spec.id
        r.label = f"{spec.platform or spec.name or spec.id} · {r.model}" if r.model else (spec.platform or spec.name)
        out.append(r)
    rep.name = spec.id
    return AdapterResult(rows=out, report=rep)
