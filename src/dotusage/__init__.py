"""dot-usage —— AI 编程工具配额看板。

把各平台的配额消耗与 token 账单汇成几张卡，推到 DOT 电子墨水屏，
同时给 macOS 菜单栏留一份随时能点开的快照。

需要 Python 3.11+（用到标准库 tomllib 读数据源配置）。
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
