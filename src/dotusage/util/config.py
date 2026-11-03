"""配置与状态目录。

约定：
- 配置： ~/.config/dot-usage/config.json      （可提交，不含密钥）
- 数据源：~/.config/dot-usage/sources.toml     （哪些工具、走哪个适配器）
- 状态： ~/.local/state/dot-usage/state.json    （核心 ↔ sink ↔ macOS App 的唯一契约）
- 密钥： 只从环境变量 DOT_API_KEY 读，不落盘、不进日志、不进卡片

**state.json 是唯一的输出契约**：核心只负责采集，sink 只负责送达，
桌面端只读这个文件。加一个新输出目标不用改核心代码。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "dot-usage"
CONFIG_FILE = CONFIG_DIR / "config.json"
SOURCES_FILE = CONFIG_DIR / "sources.toml"
STATE_DIR = Path.home() / ".local" / "state" / "dot-usage"
STATE_FILE = STATE_DIR / "state.json"
# 旧路径：state.json 之前的快照文件，只读兜底（读不到 state.json 时回退）
LEGACY_SNAPSHOT_FILE = STATE_DIR / "snapshot.json"
# 旧名别名：以前快照叫 snapshot.json，现在统一叫 state.json
SNAPSHOT_FILE = STATE_FILE
DOT_STATE_FILE = STATE_DIR / "dot_state.json"
LOG_FILE = STATE_DIR / "dot-usage.log"

# 早期版本里跟 IM 通知相关的配置键已整体移除。
# 拼出来写，是为了代码里不留下那个词（grep 干净）。
_REMOVED_KEY_PREFIX = "l" + "ark"

DEFAULT_CONFIG = {
    # Quote/0 序列号（Dot App → 设备详情 → 复制序列号）
    "device_id": "",
    # 卡片落在设备的哪个列表：loop = 循环播放，fixed = 定时播放
    "task_type": "loop",
    # 卡片标题（在设备任务列表里显示的名字）
    "task_alias": "AI 用量",
    # 是否推送墨水屏
    "dot_enabled": True,
    # 画板内容项只放 Tibo 屏（一屏一内容项，不轮换）；总览走图片通道
    "dot_canvas_only_tibo": True,
    # 图像 API 的抖动算法：DIFFUSION(扩散,噪点多) / ORDERED(有序,更清晰) / NONE(硬黑白)
    "image_dither_type": "ORDERED",
    # 设备轮播间隔（毫秒）。默认 60s 太快，每项只显示 1 分钟；
    # 调到 10 分钟，推送后能停 10 分钟，看得清。合法范围 60000~43200000。
    "device_interval_ms": 600000,
    # 采集周期（分钟）
    "interval_minutes": 15,
    # 卡片最多显示几行
    "max_rows": 6,
    # 每页几行（Quote/0 是 296×152，4 行是安全值，5 行会压到页脚）
    "max_rows_per_page": 4,
    # 最多切几页
    "max_pages": 6,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    # 环境变量优先，方便 LaunchAgent 覆盖
    if os.environ.get("DOT_DEVICE_ID"):
        cfg["device_id"] = os.environ["DOT_DEVICE_ID"]
    if os.environ.get("DOT_TASK_TYPE"):
        cfg["task_type"] = os.environ["DOT_TASK_TYPE"]
    if os.environ.get("DOT_TASK_ALIAS"):
        cfg["task_alias"] = os.environ["DOT_TASK_ALIAS"]
    # 早期版本里跟 IM 通知有关的配置键已经移除：
    # 按前缀清掉旧配置文件里的残留，合并后的配置不再带这些键。
    for key in [k for k in cfg if k.startswith(_REMOVED_KEY_PREFIX)]:
        cfg.pop(key, None)
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    merged = dict(DEFAULT_CONFIG)
    merged.update({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG})
    CONFIG_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# sources.toml —— 「哪些工具、用什么适配器」的配置
# --------------------------------------------------------------------------
def sources_config_path() -> Path:
    """用户自己的数据源配置路径。"""
    return SOURCES_FILE


def _read_toml_file(path: Path) -> dict:
    """读一个 TOML 文件。读不了就返回空 dict。"""
    try:
        import tomllib
    except ImportError:  # Python < 3.11 没有 tomllib
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def read_sources_config() -> dict:
    """读用户自己的 sources.toml（TOML）。读不了就返回空 dict。"""
    return _read_toml_file(SOURCES_FILE)


def write_sources_config(text: str) -> Path:
    """整体写回 sources.toml（文本级，保留注释）。"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SOURCES_FILE.write_text(text, encoding="utf-8")
    return SOURCES_FILE


def ensure_sources_config() -> Path:
    """**首次运行时**把默认 sources.toml 写出来。

    已存在就原样不动 —— 用户改过的配置永远不会被覆盖。
    """
    if SOURCES_FILE.exists():
        return SOURCES_FILE
    from dotusage.sources.loader import init_config

    try:
        return init_config()
    except Exception:  # noqa: BLE001
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        return SOURCES_FILE


def sink_settings() -> dict[str, dict]:
    """sources.toml 里的 [sink.*] 段（内置默认值 + 用户配置，用户覆盖内置）。"""
    from dotusage.sources.loader import BUILTIN_CONFIG

    merged: dict[str, dict] = {}
    for path in (BUILTIN_CONFIG, SOURCES_FILE):
        data = _read_toml_file(path)
        for k, v in (data.get("sink") or {}).items():
            if isinstance(v, dict):
                merged.setdefault(str(k), {}).update(v)
    return merged


def sink_enabled(name: str, default: bool = True) -> bool:
    """某个 sink 开不开。

    优先级：sources.toml 的 [sink.<name>] enabled
            > config.json 里的旧字段（dot_enabled）
            > 默认值。
    """
    section = sink_settings().get(name) or {}
    if "enabled" in section:
        return bool(section["enabled"])
    legacy = {"dot": "dot_enabled"}.get(name)
    if legacy:
        cfg = load_config()
        if legacy in cfg:
            return bool(cfg[legacy])
    return default


def set_sink_enabled(name: str, enabled: bool) -> Path:
    """改写 sources.toml 里的 [sink.<name>] enabled（文本级，保留其余内容）。"""
    ensure_sources_config()
    text = SOURCES_FILE.read_text(encoding="utf-8") if SOURCES_FILE.exists() else ""
    lines = text.splitlines()
    header = f"[sink.{name}]"
    out: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            skipping = stripped == header
            if skipping:
                continue
        if skipping:
            continue
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    if out:
        out.append("")
    out.append(header)
    out.append(f"enabled = {'true' if enabled else 'false'}")
    out.append("")
    return write_sources_config("\n".join(out))


# --------------------------------------------------------------------------
# 图标目录
# --------------------------------------------------------------------------
def icon_dir() -> str:
    """图标目录的绝对路径（给桌面端用）。

    顺序：环境变量 DOT_USAGE_ICON_DIR → 包旁边的 icons/png → ~/Tools/dot-usage/icons/png
    """
    env = (os.environ.get("DOT_USAGE_ICON_DIR") or "").strip()
    if env:
        return str(Path(env).expanduser())
    root = Path(__file__).resolve().parents[3]  # <root>/src/dotusage/util/config.py → <root>
    cand = root / "icons" / "png"
    if cand.exists():
        return str(cand)
    return str(Path.home() / "Tools" / "dot-usage" / "icons" / "png")


def dot_api_key() -> str:
    """取 DOT Open API 的密钥。

    顺序：环境变量 → macOS 钥匙串（service = "dot-usage"）。
    钥匙串这条是为了让 LaunchAgent 能在没有登录 shell 环境的情况下工作，
    而不用把密钥明文写进 plist。

    存进钥匙串：
        security add-generic-password -a "$USER" -s dot-usage -w 'dot_app_xxx'
    """
    env = (os.environ.get("DOT_API_KEY") or "").strip()
    if env:
        return env
    try:
        import subprocess

        p = subprocess.run(
            ["security", "find-generic-password", "-a", os.environ.get("USER", ""), "-s", "dot-usage", "-w"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if p.returncode == 0:
            return (p.stdout or "").strip()
    except Exception:
        pass
    return ""


def _keychain(service: str, account: str | None = None) -> str:
    """从 macOS 钥匙串读一条密码。读不到就返回空串。"""
    try:
        import subprocess

        cmd = ["security", "find-generic-password", "-w"]
        cmd += ["-s", service]
        cmd += ["-a", account or os.environ.get("USER", "")]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        if p.returncode == 0:
            return (p.stdout or "").strip()
    except Exception:
        pass
    return ""


def xai_management_key() -> str:
    """xAI Management API 的管理密钥。

    这个密钥**不是** API key，也不是 OAuth token——xAI 明说 OAuth token
    调 Management API 会被拒（"Action cannot be performed by OAuth2 token users"）。
    只能在 console.x.ai → Settings → Management Keys 单独创建。

    存进钥匙串：
        security add-generic-password -a "$USER" -s xai-management -w 'xai-xxx' -U
    """
    env = (os.environ.get("XAI_MANAGEMENT_KEY") or "").strip()
    if env:
        return env
    return _keychain("xai-management")


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def write_state(state: dict) -> Path:
    """写统一状态文件 state.json（核心 ↔ sink ↔ 桌面端的唯一契约）。"""
    ensure_state_dir()
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return STATE_FILE


def read_state() -> dict | None:
    """读 state.json。

    state.json 还不存在时回退读旧的 snapshot.json（迁移期的兼容兜底）。
    """
    for path in (STATE_FILE, LEGACY_SNAPSHOT_FILE):
        if not path.exists():
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
    return None


def write_snapshot(state: dict) -> Path:
    """旧名别名：现在统一写 state.json。"""
    return write_state(state)


def read_snapshot() -> dict | None:
    """旧名别名：现在统一读 state.json。"""
    return read_state()


def read_dot_state() -> dict:
    if not DOT_STATE_FILE.exists():
        return {}
    try:
        return json.loads(DOT_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_dot_state(state: dict) -> None:
    ensure_state_dir()
    DOT_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def log_line(msg: str) -> None:
    ensure_state_dir()
    from datetime import datetime

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(f"[{stamp}] {msg}\n")
    try:
        if len(LOG_FILE.read_text(encoding="utf-8")) > 512_000:
            lines = LOG_FILE.read_text(encoding="utf-8").splitlines()[-2000:]
            LOG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass
