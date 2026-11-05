"""本地状态文件 sink —— 写 state.json。

**state.json 是「核心 ↔ sink ↔ 桌面端」之间的唯一契约。**

- 核心只负责采集，不关心输出到哪儿
- sink 只负责送达
- 桌面端（菜单栏 App）只读这一个文件，
  所以它完全不需要知道墨水屏、密钥、推送这些概念

文件位置： ~/.local/state/dot-usage/state.json
"""

from __future__ import annotations

from ..core.schema import iso, now_ts
from ..util import config as cfgmod
from .base import Sink, register

#: 契约版本。桌面端按这个判断字段是不是老格式。
STATE_VERSION = 1


def build_state(rows, reports=None, models=None, pulse=None, projects=None,
                trend=None, cost=None) -> dict:
    """组装 state.json 的内容。

    字段就是契约本身，桌面端只认这些：

    - version        契约版本号
    - generated_at   采集时间（ISO8601）
    - icon_dir       图标目录的绝对路径（桌面端找图标用）
    - window         消耗数据的统计窗口标签（近24h / 近7天 / …）
    - platforms      按平台合并后的一行一平台
    - models         按模型维度
    - pulse          近 48 小时的消耗节奏
    - projects       按工作目录拆的账
    - daily          近 30 天按天 × 模型的消耗与折算成本
    - cost           当前窗口的等效成本（按官方 API 单价折算）
    - sources        各数据源的采集状态
    - rows           原始明细（排查用）
    """
    from ..core.aggregate import aggregate

    # 消耗行的窗口标签。桌面端要显示「近24h」还是「近7天」，
    # 靠这个字段——不然它只能写死「今天」，窗口一改就说谎了。
    wins = {
        str(getattr(r, "window", "") or "")
        for r in (rows or [])
        if getattr(r, "kind", "") == "consumption" and (getattr(r, "total_tokens", 0) or 0) > 0
    }
    wins.discard("")
    window = next(iter(wins)) if len(wins) == 1 else ""

    return {
        "version": STATE_VERSION,
        "generated_at": iso(now_ts()),
        "generated_at_epoch": now_ts(),
        "icon_dir": cfgmod.icon_dir(),
        "window": window,
        "platforms": [r.to_dict() for r in aggregate(rows)],
        "models": list(models or []),
        "pulse": pulse or {},
        "projects": projects or {},
        "daily": trend or {},
        "cost": cost or {},
        "sources": [rep.to_dict() for rep in (reports or [])],
        "rows": [r.to_dict() for r in rows],
    }


class LocalStateSink(Sink):
    """把采集结果写成本地状态文件。"""

    name = "local"
    label = "本地状态文件"

    def enabled(self, cfg: dict) -> bool:
        # state.json 是桌面端的数据来源，关不掉
        return True

    def push(self, rows, cfg: dict, state: dict, force: bool = False) -> str:
        path = cfgmod.write_state(state)
        return f"状态已写入 {path}"


register(LocalStateSink())
