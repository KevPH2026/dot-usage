"""dot-usage 命令行入口。

    python3 -m dotusage.cli collect    # 只采集，写 state.json
    python3 -m dotusage.cli run        # 采集 → 写 state.json → 推墨水屏
    python3 -m dotusage.cli preview    # 生成 HTML 预览
    python3 -m dotusage.cli show       # 打印当前状态
    python3 -m dotusage.cli doctor     # 自检：配置 / 密钥 / 设备 / 输出目标
    python3 -m dotusage.cli sources    # 看看现在有哪些数据源
    python3 -m dotusage.cli sinks      # 看看各输出目标开不开
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from dotusage.util import config as cfgmod
from dotusage.sources.builtin import collect_all_with_official
from dotusage.cards.preview import write_preview
from dotusage.sinks import run_sinks, sink_status
from dotusage.sinks.base import PushError
from dotusage.sinks.dot import dot_devices, find_content_keys, push_dot_smart, switch_next
from dotusage.cards.canvas import build_pages, summarize
from dotusage.core.schema import UsageRow, now_ts


def _rows_from_state(state: dict) -> list[UsageRow]:
    rows = []
    for r in state.get("rows", []):
        rows.append(UsageRow(**{k: v for k, v in r.items() if k in UsageRow.__dataclass_fields__}))
    return rows


def do_collect() -> tuple[list[UsageRow], dict]:
    """采集一遍，组装 state.json 的内容（不落盘，落盘交给 local sink）。"""
    from dotusage.core.aggregate import aggregate
    from dotusage.core.models import collect_model_usage
    from dotusage.sinks.local import build_state

    # 首次运行：把默认 sources.toml 写出来，用户直接改就行
    cfgmod.ensure_sources_config()

    rows, reports = collect_all_with_official()

    try:
        models = [u.to_dict() for u in collect_model_usage()]
    except Exception as e:  # noqa: BLE001
        cfgmod.log_line(f"model usage collect failed: {e}")
        models = []

    # 项目账（按工作目录拆 Codex 的消耗）
    projects: dict = {}
    try:
        from dotusage.core.projects import collect_projects

        projects = collect_projects()
    except Exception as e:  # noqa: BLE001
        cfgmod.log_line(f"projects collect failed: {e}")

    # 24 小时消耗节奏（给「用量脉搏」卡）
    pulse: dict = {}
    try:
        from dotusage.core.pulse import collect_hourly, snapshot_payload

        pulse = snapshot_payload(collect_hourly(48))
    except Exception as e:  # noqa: BLE001
        cfgmod.log_line(f"pulse collect failed: {e}")

    state = build_state(rows, reports, models, pulse, projects)
    return rows, state


def _print_sink_results(results) -> None:
    """把各 sink 的结果打出来（成功打勾、跳过打点、失败打叉）。"""
    for r in results:
        if not r.ok:
            print(f"✗ {getattr(r, 'label', None) or r.name}：{r.message}", file=sys.stderr)
            cfgmod.log_line(f"sink {r.name} failed: {r.message}")
        elif r.skipped:
            print(f"· {getattr(r, 'label', None) or r.name}：{r.message}")
        else:
            print(f"✓ {r.message}")


def cmd_collect(args) -> int:
    rows, state = do_collect()
    cfg = cfgmod.load_config()
    results = run_sinks(rows, cfg, state, overrides={"dot": False})
    print(f"✓ 采集完成（{len(rows)} 行）")
    _print_sink_results(results)
    for rep in state["sources"]:
        flag = "✓" if rep["ok"] else "✗"
        extra = rep["error"] or rep["note"] or ""
        print(f"  {flag} {rep['name']:<9} {rep['rows']} 行  {extra}")
    print()
    print(summarize(rows))
    return 0 if all(r.ok for r in results) else 1


def _fingerprint(rows: list[UsageRow]) -> str:
    """摘要指纹，用来判断「内容有没有变」。

    注意别把倒计时算进去——那每分钟都在变，会让指纹永远不同。
    """
    from dotusage.cards.canvas import select_rows

    parts = []
    for r in select_rows(rows):
        pct = round(r.used_percent) if r.used_percent is not None else "-"
        parts.append(f"{r.source}|{r.label}|{r.headline}|{pct}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _sync_device_settings(cfg: dict) -> None:
    """把配置里的轮播间隔同步到设备（幂等，变化了才写）。"""
    want = cfg.get("device_interval_ms")
    if not want:
        return
    try:
        from dotusage.sinks.dot import _dot_request

        key = cfgmod.dot_api_key()
        if not key:
            return
        cur = _dot_request("GET", f"/api/authV2/open/device/{cfg['device_id']}/settings", key) or {}
        iv = cur.get("interval") or {}
        if int(iv.get("powerMs") or 0) == int(want) and int(iv.get("batteryMs") or 0) == int(want):
            return
        _dot_request(
            "POST", f"/api/authV2/open/device/{cfg['device_id']}/settings", key,
            {"interval": {"powerMs": int(want), "batteryMs": int(want)}},
        )
        print(f"· 设备轮播间隔 → {int(want) // 60000} 分钟")
    except Exception as e:  # noqa: BLE001
        cfgmod.log_line(f"device settings sync failed: {e}")


def cmd_run(args) -> int:
    rows, state = do_collect()
    cfg = cfgmod.load_config()
    print(f"✓ 采集完成（{len(rows)} 行）")

    ok = True
    if args.dot:
        _sync_device_settings(cfg)
    results = run_sinks(rows, cfg, state, overrides={"dot": bool(args.dot)})
    _print_sink_results(results)

    cfgmod.log_line(f"run ok={ok} rows={len(rows)}")
    return 0 if ok else 1


def cmd_pages(args) -> int:
    rows, _ = do_collect()
    cfg = cfgmod.load_config()
    pages = build_pages(
        rows,
        max_rows=cfg.get("max_rows_per_page"),
        max_pages=cfg.get("max_pages"),
        device_note=cfg["device_id"][-4:],
    )
    print(f"共 {len(pages)} 屏：\n")
    for i, p in enumerate(pages):
        d = p["data"]
        print(f"第 {i + 1} 屏 [{d['title']}]")
        for r in d.get("rows", []):
            print(f"    {r['label']:<24} {r['headline']:>14}  {r['detail']}")
        print()
    print("要设备真正轮播：Dot. App → Content Studio 里给这台设备建同样数量的「Canvas API」内容项。")
    print("只有一个 Canvas 项也没关系——脚本会自动改成每次轮换写一页。")
    return 0


def cmd_switch(args) -> int:
    cfg = cfgmod.load_config()
    try:
        print("✓ " + switch_next(cfg))
    except PushError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    return 0


def cmd_preview(args) -> int:
    rows, _ = do_collect()
    out = Path(args.out).expanduser()
    write_preview(rows, out, rows)
    print(f"✓ 预览已生成 → {out}")
    return 0


def cmd_open(args) -> int:
    """用 deep link 把 DOT App 送到内容工坊的对应内容项。

    添加内容项这一步必须用户手动点（App 界面在 RN WebView 里，
    没有可被 AppleScript 点的元素树），但导航可以自动完成。
    """
    import subprocess

    targets = {
        "image": "dot:///studio/detail/mindreset.photo",
        "canvas": "dot:///studio/detail/mindreset.canvas_api",
        "studio": "dot:///studio/list",
    }
    keys = ["image", "canvas"] if args.what == "both" else [args.what]
    for k in keys:
        subprocess.run(["open", targets[k]], check=False)
        print(f"✓ 已打开 {k} → {targets[k]}")
        if len(keys) > 1:
            import time

            time.sleep(3)
    print("请在 DOT App 里点「添加到设备」完成绑定")
    return 0


def cmd_check(args) -> int:
    """推送一次并抓设备真实渲染图（转正后保存），自己验收排版。"""
    cfg = cfgmod.load_config()
    _sync_device_settings(cfg)
    try:
        msg = push_dot_smart(_rows_cache(), cfg)
        print(f"✓ {msg}")
    except PushError as e:
        print(f"✗ 推送失败：{e}", file=sys.stderr)
        return 1
    try:
        from dotusage.util.device_check import capture_device_render

        out = capture_device_render(wait=args.wait, device_id=cfg.get("device_id"))
        print(f"✓ 设备实渲染图（已转正）→ {out}")
    except Exception as e:  # noqa: BLE001
        print(f"· 抓渲染图失败：{e}")
    return 0


def _rows_cache() -> list:
    """重新采集一次（check 命令要推最新的）。"""
    rows, _ = collect_all_with_official()
    return rows


def cmd_show(args) -> int:
    state = cfgmod.read_state()
    if not state:
        print("还没有状态文件，先跑 collect")
        return 1
    print(f"生成于 {state.get('generated_at')}")
    print()
    print(summarize(_rows_from_state(state)))
    return 0


def cmd_sources(args) -> int:
    """列出生效的数据源（id / 适配器 / 平台 / 官方配额 provider / 统计窗口）。"""
    from dotusage.sources import loader as srcloader
    from dotusage.sources.window import parse_window

    if args.init:
        path = cfgmod.ensure_sources_config()
        if args.force:
            path = srcloader.init_config(force=True)
        print(f"✓ 默认数据源配置已就位 → {path}")
        print("  改完跑 `python3 -m dotusage.cli sources` 看效果。")
        return 0

    specs = srcloader.load_config()
    print(f"生效的数据源（{len(specs)} 个）：")
    print(f"  {'ID':<12}{'适配器':<14}{'平台':<14}{'官方配额':<12}{'窗口':<10}状态")
    for s in specs:
        win = ""
        if s.options.get("window"):
            _, win = parse_window(s.options["window"])
        elif s.adapter == "builtin":
            win = "近24h"
        print(
            f"  {s.id:<12}{s.adapter:<14}{(s.platform or '-'):<14}"
            f"{(s.official or '-'):<12}{(win or '-'):<10}{'启用' if s.enabled else '停用'}"
        )
    path = cfgmod.sources_config_path()
    print()
    print(f"配置文件：{path}{'' if path.exists() else '（还没有，跑 sources --init 生成）'}")
    print("改窗口：在对应源下面加 [source.options] window = \"7d\"（支持 24h/today/7d/30d/all）")
    return 0


def cmd_sinks(args) -> int:
    """打印各输出目标（sink）的开关状态。"""
    cfg = cfgmod.load_config()
    rows = sink_status(cfg)
    print(f"输出目标（sink）：{len(rows)} 个")
    for r in rows:
        flag = "✓" if r["enabled"] else "·"
        state = "开" if r["enabled"] else "关"
        err = f"  ({r['error']})" if r["error"] else ""
        print(f"  {flag} {r['name']:<8}{r['label']:<14}{state}{err}")
    print()
    print("开关写在 sources.toml 的 [sink.<名字>] enabled 里；")
    print(f"  {cfgmod.sources_config_path()}")
    return 0


def cmd_doctor(args) -> int:
    cfg = cfgmod.load_config()
    print("—— 配置 ——")
    print(f"  设备序列号   {cfg['device_id']}")
    print(f"  内容列表     {cfg['task_type']}   标题 {cfg['task_alias']}")
    print(f"  采集周期     {cfg['interval_minutes']} 分钟")

    print("—— 密钥 ——")
    key = cfgmod.dot_api_key()
    print(f"  DOT_API_KEY  {'已设置 (' + key[:10] + '…)' if key else '未设置（墨水屏推送会被跳过）'}")

    print("—— 状态文件 ——")
    state = cfgmod.read_state()
    if state:
        print(f"  ✓ {cfgmod.STATE_FILE}")
        print(f"    生成于 {state.get('generated_at')}  契约版本 {state.get('version', '?')}")
        print(f"    图标目录 {state.get('icon_dir')}")
    else:
        print(f"  · 还没有 {cfgmod.STATE_FILE}，跑一次 collect")

    print("—— 输出目标 ——")
    for r in sink_status(cfg):
        print(f"  {'✓' if r['enabled'] else '·'} {r['name']:<8}{r['label']:<14}{'开' if r['enabled'] else '关'}")

    print("—— 墨水屏 ——")
    if key:
        try:
            devs = dot_devices(key)
            for d in devs:
                print(f"  ✓ 设备 {d.get('id')} ({d.get('series')}/{d.get('model')} ed{d.get('edition')}) {d.get('alias','')}")
        except PushError as e:
            print(f"  ✗ {e}")
        # 内容项盘点：设备 loop 里有几类可写的内容项、几个是我自己的卡
        try:
            from dotusage.sinks.dot import _dot_request

            raw = _dot_request("GET", f"/api/authV2/open/device/{cfg['device_id']}/loop/list", key)
            items = raw if isinstance(raw, list) else (raw.get("result") or [])
            tt = cfg.get("task_type", "loop")
            canvases = find_content_keys(key, cfg["device_id"], tt, "CANVAS")
            texts = find_content_keys(key, cfg["device_id"], tt, "TEXT")
            images = find_content_keys(key, cfg["device_id"], tt, "IMAGE")
            print(f"  · 循环列表共 {len(items)} 项（画板 {len(canvases)} / 图像 {len(images)} / 文本 {len(texts)}）")
            try:
                st = _dot_request("GET", f"/api/authV2/open/device/{cfg['device_id']}/settings", key) or {}
                iv = (st.get("interval") or {}).get("powerMs")
                if iv:
                    print(f"  · 轮播间隔 {int(iv) // 60000} 分钟（列表里 {len(items)} 项，一轮约 {len(items) * int(iv) // 60000} 分钟）")
            except Exception:
                pass
            if canvases:
                print("  ✓ 卡1 Tibo 重置 → 画板 API（超大 YES/NO）")
            elif texts:
                print("  ✓ 卡1 Tibo 重置 → 文本 API（超大 YES/NO）")
            else:
                print("  ✗ 卡1 Tibo 重置 → 需要一个「画板 API」或「文本 API」内容项")
            # 自查：图像项的标题（= 我推进去的卡名）是不是齐全、有没有重复
            try:
                from dotusage.sinks.dot_app import call as app_call

                st, raw = app_call(f"/api/authV2/user/device/{cfg['device_id']}/combine?project=staticData")
                tl = (json.loads(raw).get("taskData") or {}).get("loop", {}).get("taskList") or []
                titles = [str(it.get("title") or "") for it in tl
                          if str(it.get("id", "")).startswith("mindreset.image_api")]
                want = ["AI 用量 消耗", "用量脉搏", "项目烧钱榜", "官方重置时间", "额度窗口"]
                missing = [w for w in want if w not in titles]
                dup = {t for t in titles if titles.count(t) > 1}
                if missing:
                    print(f"  ✗ 图像项缺卡：{'、'.join(missing)}（推送时该卡渲染失败）")
                if dup:
                    print(f"  ✗ 图像项标题重复：{'、'.join(sorted(dup))}")
                if not missing and not dup:
                    print(f"  ✓ 图像项 {len(titles)} 张，卡名齐全无重复")
            except Exception as e:  # noqa: BLE001
                print(f"  · 图像项自查失败：{str(e)[:60]}")

            if images:
                print("  ✓ 卡2 用量消耗 → 图像 API（一屏总览）")
                if len(images) >= 2:
                    print(f"  ✓ 卡3 用量脉搏 → 图像 API（24h 节奏，共 {len(images)} 个图像项）")
                if len(images) >= 3:
                    print("  ✓ 卡4 项目烧钱榜 → 图像 API（按工作目录拆账）")
                if len(images) >= 4:
                    print("  ✓ 卡5 官方重置时间 → 图像 API（官方下发的重置时刻）")
                if len(images) >= 5:
                    print("  ✓ 卡6 额度窗口 → 图像 API（5 小时 + 周 并排）")
            elif len(canvases) >= 2:
                print("  ✓ 卡2 用量消耗 → 画板 API")
            else:
                print("  ✗ 卡2 用量消耗 → 需要一个「图像 API」或第 2 个「画板 API」内容项")
            if texts:
                print("  ✓ 卡7 今日速览 → 文本 API（今日总量 + 最费模型）")
            else:
                print("  · 卡7 今日速览 → 没有「文本 API」内容项，跳过")

        except PushError as e:
            print(f"  ✗ 内容列表读取失败：{e}")
    else:
        print("  · 跳过（没有密钥）")
    return 0


def cmd_config(args) -> int:
    cfg = cfgmod.load_config()
    if args.set:
        for kv in args.set:
            k, _, v = kv.partition("=")
            cfg[k] = v
        cfgmod.save_config(cfg)
        print(f"✓ 已写入 {cfgmod.CONFIG_FILE}")
    print(json.dumps(cfg, ensure_ascii=False, indent=2))
    return 0


def _check_python() -> None:
    """Python 3.11 以下直接拦下来，别让它半死不活地跑。"""
    if sys.version_info < (3, 11):
        sys.stderr.write(
            f"dot-usage 需要 Python 3.11+，当前是 {sys.version.split()[0]}"
            f"（{sys.executable}）。\n"
            "换 3.11+ 的解释器重跑，例如 python3.12 -m dotusage.cli ...\n"
        )
        raise SystemExit(2)


def main(argv=None) -> int:
    _check_python()
    p = argparse.ArgumentParser(prog="dot-usage", description="AI 用量 → 墨水屏 / 本地状态")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="采集并推送")
    r.add_argument("--no-dot", dest="dot", action="store_false", help="不推墨水屏")
    r.set_defaults(func=cmd_run)

    oc = sub.add_parser("open", help="用 deep link 打开 DOT App 的内容工坊")
    oc.add_argument("what", nargs="?", default="studio",
                    choices=["studio", "image", "canvas", "both"], help="要打开的内容项")
    oc.set_defaults(func=cmd_open)

    ck = sub.add_parser("check", help="推送并抓设备真实渲染图（自查排版）")
    ck.add_argument("--wait", type=float, default=6.0, help="推送后等设备刷新的秒数")
    ck.set_defaults(func=cmd_check)

    sw = sub.add_parser("switch", help="让设备立刻切到下一屏")
    sw.set_defaults(func=cmd_switch)

    pg = sub.add_parser("pages", help="打印本地会分成哪几页")
    pg.set_defaults(func=cmd_pages)

    c = sub.add_parser("collect", help="只采集")
    c.set_defaults(func=cmd_collect)

    v = sub.add_parser("preview", help="生成 HTML 预览")
    v.add_argument("--out", default="~/Tools/dot-usage/preview/card-preview.html")
    v.set_defaults(func=cmd_preview)

    s = sub.add_parser("show", help="打印当前状态")
    s.set_defaults(func=cmd_show)

    d = sub.add_parser("doctor", help="自检")
    d.set_defaults(func=cmd_doctor)

    src = sub.add_parser("sources", help="列出生效的数据源")
    src.add_argument("--init", action="store_true", help="把默认数据源配置写到 ~/.config/dot-usage/sources.toml")
    src.add_argument("--force", action="store_true", help="配合 --init 覆盖已有配置")
    src.set_defaults(func=cmd_sources)

    sk = sub.add_parser("sinks", help="打印各输出目标的开关状态")
    sk.set_defaults(func=cmd_sinks)

    cf = sub.add_parser("config", help="查看/修改配置")
    cf.add_argument("--set", action="append", help="key=value，可重复")
    cf.set_defaults(func=cmd_config)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
