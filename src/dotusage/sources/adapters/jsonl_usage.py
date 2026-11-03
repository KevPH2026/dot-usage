"""通用 JSONL 适配器：每行一个 JSON 的会话日志。

配置示例（Claude Code 就是这种）：

    - id: claude
      name: Claude Code
      platform: Claude
      adapter: jsonl_usage
      options:
        glob: "~/.claude/projects/**/*.jsonl"
        model: "message.model"
        input: "message.usage.input_tokens"
        output: "message.usage.output_tokens"
        cache_read: "message.usage.cache_read_input_tokens"
        cache_write: "message.usage.cache_creation_input_tokens"
        ts: "timestamp"

字段用点号路径（`a.b.c`），数组用 `a.0.b`。
时间是 ISO 字符串（`2026-10-29T08:03:04.862Z`）或秒/毫秒时间戳。
"""

from __future__ import annotations

import glob as globmod
import json
from collections import defaultdict
from datetime import datetime

from ...core.schema import now_ts, to_epoch
from ..base import AdapterResult, SourceSpec, expand, report_for, row_from_fields


def _dig(obj, path: str):
    """按点号路径取值，取不到返回 None。"""
    if not path:
        return None
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _ts_of(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v) / 1000 if float(v) > 1e11 else float(v)
    s = str(v)
    try:
        return to_epoch(s.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return 0.0


def collect(spec: SourceSpec) -> AdapterResult:
    opts = spec.options
    pattern = str(expand(opts.get("glob", "")))
    if not pattern:
        return AdapterResult(report=report_for(spec, [], "缺少 options.glob"))

    window = float(opts.get("window_hours", 24) or 0)
    since = (now_ts() - window * 3600) if window else 0.0
    max_files = int(opts.get("max_files", 400) or 400)

    try:
        files = sorted(globmod.glob(pattern, recursive=True), key=lambda p: _mtime(p), reverse=True)
    except Exception as e:  # noqa: BLE001
        return AdapterResult(report=report_for(spec, [], f"扫描失败 {e}"))

    agg: dict[str, dict] = defaultdict(lambda: {"input": 0, "output": 0, "reasoning": 0,
                                                "cache_read": 0, "cache_write": 0, "total": 0,
                                                "calls": 0, "ts": 0.0, "cost": 0.0})
    scanned = 0
    for path in files[:max_files]:
        if window and _mtime(path) < since:
            continue
        scanned += 1
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"usage"' not in line and '"tokens"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    usage = _dig(d, opts.get("usage_path", "")) if opts.get("usage_path") else d
                    model = str(_dig(usage, opts.get("model", "")) or opts.get("fallback_model") or "默认")
                    ts = _ts_of(_dig(d, opts.get("ts", "")))
                    if window and ts and ts < since:
                        continue
                    a = agg[model]
                    for key in ("input", "output", "reasoning", "cache_read", "cache_write", "total", "cost"):
                        p = opts.get(key)
                        if not p:
                            continue
                        v = _dig(usage, p)
                        if v is None:
                            continue
                        try:
                            a[key] += float(v)
                        except (TypeError, ValueError):
                            continue
                    a["calls"] += 1
                    if ts:
                        a["ts"] = max(a["ts"], ts)
        except Exception:  # noqa: BLE001
            continue

    rows = []
    for model, a in agg.items():
        total = a["total"] or (a["input"] + a["output"] + a["cache_read"] + a["cache_write"])
        if total <= 0:
            continue
        rows.append(row_from_fields(spec, model, a))
    rows.sort(key=lambda x: -x.total_tokens)
    rep = report_for(spec, rows)
    if not rows:
        rep.note = f"24h 内无记录（扫了 {scanned} 个文件）"
    return AdapterResult(rows=rows, report=rep)


def _mtime(p: str) -> float:
    import os

    try:
        return os.path.getmtime(p)
    except OSError:
        return 0.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat()
