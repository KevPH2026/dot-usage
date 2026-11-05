"""数据源加载器。

配置位置（按顺序，后者覆盖前者）：
    1. 包内置的 `sources/defaults.toml`（作者本机的工具）
    2. `~/.config/dot-usage/sources.toml`（你自己的）

配置格式用 TOML（Python 3.11+ 标准库 tomllib，零第三方依赖），
也接受同目录的 `sources.json`。

    [[source]]
    id = "mytool"
    name = "MyTool"
    platform = "自家"
    icon = "chatgpt"
    adapter = "sqlite_usage"     # builtin / sqlite_usage / jsonl_usage

    [source.options]
    db = "~/.mytool/data.db"
    sql = '
        SELECT model AS model, ts AS ts,
               input_tokens AS input, output_tokens AS output,
               total_tokens AS total
        FROM usage WHERE ts > :since
    '
    time_scale = 0.001
    window_hours = 24

命令行：
    python -m dotusage sources            # 看当前有哪些源
    python -m dotusage sources --init     # 把默认配置写到 ~/.config
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from ..core.schema import SourceReport, UsageRow
from .base import AdapterResult, SourceSpec

BUILTIN_CONFIG = Path(__file__).with_name("defaults.toml")


def config_dir() -> Path:
    return Path.home() / ".config" / "dot-usage"


def user_config() -> Path:
    d = config_dir()
    if (d / "sources.json").exists():
        return d / "sources.json"
    return d / "sources.toml"


def _read_toml(path: Path) -> dict:
    """读 TOML。读不了就大声报错——绝不能静默返回空配置。

    静默失败过一次：解释器是 Python 3.9（没有 tomllib），
    配置被读成空，采集还「成功」了，只是没有数据源。
    """
    try:
        import tomllib
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "dot-usage 需要 Python 3.11+（数据源配置用标准库 tomllib 读）。"
            f"当前解释器是 {sys.version.split()[0]}（{sys.executable}），换 3.11+ 的再跑。"
        ) from e

    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except Exception as e:
        raise RuntimeError(f"数据源配置读不了：{path}（{e}）") from e


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _merge_spec(base: dict, over: dict) -> dict:
    """合并两层配置：用户层只覆盖它**显式写了**的字段。

    为什么要深合并 options：用户配置里写了一个源，通常只想改一两项
    （比如 window = "7d"），不该把内置配置里的其它默认值一起抹掉。
    浅合并会让「加一个 window」变成「把整个源重置」。
    """
    out = dict(base)
    for k, v in over.items():
        if k == "options" and isinstance(v, dict) and isinstance(out.get("options"), dict):
            merged = dict(out["options"])
            merged.update(v)
            out["options"] = merged
        else:
            out[k] = v
    return out


def load_config() -> list[SourceSpec]:
    """读配置 → 合并去重 → 返回源列表。"""
    merged: dict[str, dict] = {}

    def apply(data: dict) -> None:
        for item in data.get("source") or []:
            if isinstance(item, dict) and item.get("id"):
                sid = str(item["id"])
                merged[sid] = _merge_spec(merged[sid], item) if sid in merged else dict(item)

    for path, reader in ((BUILTIN_CONFIG, _read_toml), (user_config(), _read_toml)):
        if path.exists():
            apply(reader(path))

    ucfg = user_config()
    if ucfg.exists() and ucfg.suffix == ".json":
        apply(_read_json(ucfg))

    return [SourceSpec.from_dict(v) for v in merged.values()]


def init_config(force: bool = False) -> Path:
    """把默认配置写到用户配置目录。"""
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    dst = d / "sources.toml"
    if dst.exists() and not force:
        return dst
    shutil.copy2(BUILTIN_CONFIG, dst)
    return dst


def collect_sources(specs: list[SourceSpec] | None = None) -> tuple[list[UsageRow], list[SourceReport]]:
    """跑所有启用的源。"""
    from .adapters import get as get_adapter
    from .builtin import collect_builtin

    specs = specs if specs is not None else load_config()
    rows: list[UsageRow] = []
    reports: list[SourceReport] = []

    for spec in specs:
        if not spec.enabled or not spec.id:
            continue
        try:
            if spec.adapter == "builtin":
                r, rep = collect_builtin(spec.builtin or spec.id, spec)
                res = AdapterResult(rows=r, report=rep)
            elif spec.adapter == "official":
                # 官方 provider 由 collect_official() 统一跑一遍（按配置点名），
                # 这里不重复跑，否则同一批行会出两遍。
                continue
            else:
                fn = get_adapter(spec.adapter)
                if fn is None:
                    rep = SourceReport(spec.id)
                    rep.ok = False
                    rep.error = f"没有这个适配器：{spec.adapter}"
                    reports.append(rep)
                    continue
                res = fn(spec)
        except Exception as e:  # noqa: BLE001
            rep = SourceReport(spec.id)
            rep.ok = False
            rep.error = f"采集异常 {e}"
            reports.append(rep)
            continue
        rows.extend(res.rows)
        if res.report:
            reports.append(res.report)

    return rows, reports


def official_provider_names(specs: list[SourceSpec] | None = None) -> list[str]:
    """配置里点名要跑哪些官方配额 provider。"""
    specs = specs if specs is not None else load_config()
    out: list[str] = []
    for s in specs:
        if s.enabled and s.official and s.official not in out:
            out.append(s.official)
    return out
