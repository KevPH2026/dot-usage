"""通用 SQLite 适配器。

**绝大多数 AI CLI 工具的用量都记在某个 SQLite 表里**，
所以这个适配器能接下大部分工具：写一条 SQL + 一份字段映射就行。

配置示例：

    - id: mytool
      name: MyTool
      platform: 自家
      adapter: sqlite_usage
      options:
        db: "~/.mytool/data.db"
        # :since 会被替换成窗口起点（秒）
        sql: |
          SELECT model AS model, ts AS ts,
                 input_tokens AS input, output_tokens AS output,
                 total_tokens AS total, cost_usd AS cost
          FROM usage
          WHERE ts > :since
        time_scale: 0.001     # 时间戳单位换算（毫秒 → 秒填 0.001）
        window_hours: 24      # 只看最近 24 小时；填 0 表示全部历史
        label_model: true     # 标题带模型名
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from ...core.schema import now_ts
from ..base import AdapterResult, SourceSpec, expand, report_for, row_from_fields

FIELDS = ("model", "input", "output", "reasoning", "cache_read", "cache_write",
          "total", "calls", "ts", "cost")


def collect(spec: SourceSpec) -> AdapterResult:
    opts = spec.options
    db = expand(opts.get("db", ""))
    if not db.exists():
        return AdapterResult(report=report_for(spec, [], f"未找到 {db}"))
    sql = opts.get("sql") or ""
    if not sql.strip():
        return AdapterResult(report=report_for(spec, [], "缺少 options.sql"))

    window = float(opts.get("window_hours", 24) or 0)
    since = (now_ts() - window * 3600) if window else 0
    scale = float(opts.get("time_scale", 1) or 1)

    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            raw = con.execute(sql, {"since": since}).fetchall()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        return AdapterResult(report=report_for(spec, [], f"查询失败 {e}"))

    agg: dict[str, dict] = defaultdict(lambda: {k: 0 for k in FIELDS})
    for r in raw:
        d = dict(r)
        model = str(d.get("model") or opts.get("fallback_model") or "默认")
        a = agg[model]
        for k in FIELDS:
            if k in ("model", "ts"):
                continue
            v = d.get(k)
            if v is None:
                continue
            try:
                a[k] += float(v)
            except (TypeError, ValueError):
                continue
        ts = d.get("ts")
        if ts:
            try:
                a["ts"] = max(a["ts"], float(ts) * scale)
            except (TypeError, ValueError):
                pass

    rows = []
    for model, a in agg.items():
        total = a["total"] or (a["input"] + a["output"] + a["cache_read"] + a["cache_write"])
        if total <= 0:
            continue
        rows.append(row_from_fields(spec, model, a))
    rows.sort(key=lambda x: -x.total_tokens)
    return AdapterResult(rows=rows, report=report_for(spec, rows))
