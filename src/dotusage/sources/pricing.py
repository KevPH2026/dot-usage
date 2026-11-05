"""价格估算：把 token 折算成钱。

**为什么需要**：本地日志只记 token，不记钱——因为大部分人用的是订阅制
（M Plan / GLM Coding Plan / ChatGPT Plus），钱是包月付的，工具自己也不知道
「这次调用值多少钱」。

但「这些 token 如果按 API 买要多少钱」是个很有用的参考：
判断订阅买得值不值、哪个模型最烧钱。所以这里用**官方公开单价**折算。

**折算值 ≠ 你账单上的钱**。用订阅时你付的是月费，这���只是「等效 API 价」。

单价表会过时，所以：
1. 内置表写在下面，标注了抓取日期
2. 用户可以在 `sources.toml` 的 `[pricing.<模型前缀>]` 里覆盖

单价单位：**每百万 token**。美元价格按汇率折成人民币（`FX_USD_CNY`）。
"""

from __future__ import annotations

from typing import Any

#: 美元兑人民币汇率。折算只是给个量级，不精确。
FX_USD_CNY = 7.1

#: 内置单价表（抓取日期 2026-11-05）。
#: 键是模型名的**前缀**（小写），匹配时取最长前缀命中。
#: 值为每百万 token 的价格 + 币种。
DEFAULT_PRICES: dict[str, dict[str, Any]] = {
    # ---- MiniMax 中国站（platform.minimaxi.com/docs/pricing/overview）----
    "minimax-m3.1-flash": {"input": 1.00, "output": 4.00, "cache_read": 0.10, "currency": "CNY"},
    "minimax-m3": {"input": 2.10, "output": 8.40, "cache_read": 0.42, "currency": "CNY"},
    "minimax-m2.7": {"input": 1.20, "output": 4.80, "cache_read": 0.12, "currency": "CNY"},
    # ---- 智谱 GLM（docs.bigmodel.cn/cn/guide/start/pricing）----
    "glm-5.3": {"input": 8.0, "output": 28.0, "cache_read": 2.0, "currency": "CNY"},
    "glm-5.3-flash": {"input": 0.8, "output": 2.8, "cache_read": 0.23, "currency": "CNY"},
    "glm-5.2": {"input": 8.0, "output": 28.0, "cache_read": 2.0, "currency": "CNY"},
    "glm-5": {"input": 4.0, "output": 18.0, "cache_read": 1.0, "currency": "CNY"},
    # ---- DeepSeek（api-docs.deepseek.com，取非高峰价）----
    "deepseek-v4-pro": {"input": 0.66, "output": 1.98, "cache_read": 0.022, "currency": "USD"},
    "deepseek-flash": {"input": 0.15, "output": 0.60, "cache_read": 0.003, "currency": "USD"},
    # ---- OpenAI（developers.openai.com/api/docs/pricing，标准档）----
    "gpt-6-astra": {"input": 10.00, "output": 50.00, "cache_read": 1.00, "currency": "USD"},
    "gpt-6.1-sol": {"input": 2.00, "output": 10.00, "cache_read": 0.10, "currency": "USD"},
    "gpt-6.1-terra": {"input": 1.00, "output": 5.00, "cache_read": 0.05, "currency": "USD"},
    "gpt-6-sol": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "currency": "USD"},
    "gpt-6-luna": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "currency": "USD"},
    "gpt-5.6-sol": {"input": 4.00, "output": 20.00, "cache_read": 0.40, "currency": "USD"},
    "gpt-5.6-terra": {"input": 2.00, "output": 12.00, "cache_read": 0.20, "currency": "USD"},
    "gpt-5.6-luna": {"input": 0.20, "output": 1.20, "cache_read": 0.02, "currency": "USD"},
    "gpt-5.3-codex": {"input": 1.75, "output": 14.00, "cache_read": 0.175, "currency": "USD"},
}

#: 缓存里用户覆盖的单价（进程内缓存，避免每次都读文件）
_OVERRIDES: dict[str, dict[str, Any]] | None = None


def _norm(model: str) -> str:
    return str(model or "").strip().lower()


def price_table() -> dict[str, dict[str, Any]]:
    """内置单价 + 用户覆盖。"""
    global _OVERRIDES
    if _OVERRIDES is None:
        _OVERRIDES = {}
        try:
            from .loader import pricing_overrides

            _OVERRIDES = pricing_overrides()
        except Exception:  # noqa: BLE001
            _OVERRIDES = {}
    out = {k: dict(v) for k, v in DEFAULT_PRICES.items()}
    for k, v in _OVERRIDES.items():
        if isinstance(v, dict):
            merged = dict(out.get(k, {}))
            merged.update(v)
            out[k] = merged
    return out


def reset_cache() -> None:
    """改完配置后清缓存。"""
    global _OVERRIDES
    _OVERRIDES = None


def price_for(model: str) -> dict[str, Any] | None:
    """按模型名找单价。取**最长前缀**命中（glm-5.3-flash 优先于 glm-5.3）。"""
    name = _norm(model)
    if not name:
        return None
    table = price_table()
    best_key = None
    for key in table:
        if name.startswith(key) and (best_key is None or len(key) > len(best_key)):
            best_key = key
    if best_key is None:
        return None
    p = table[best_key]
    return {
        "key": best_key,
        "input": float(p.get("input") or 0),
        "output": float(p.get("output") or 0),
        "cache_read": float(p.get("cache_read") or 0),
        "currency": str(p.get("currency") or "CNY"),
    }


def _to_cny(value: float, currency: str) -> float:
    if currency.upper() == "USD":
        return value * FX_USD_CNY
    return value


def estimate(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_included: bool = False,
) -> float | None:
    """折算成**人民币元**。认不出模型就返回 None（不瞎猜）。

    `cache_included` 很关键：
    **有些工具的 input_tokens 已经包含 cache_read**（OpenAI 语义，
    ZCode 的 model_usage 和 Codex 的 last_token_usage 都是这样），
    这时候缓存那部分要按缓存单价单独算，不能既算输入价又算缓存价——
    否则会高估好几倍。

    两种语义：
    - cache_included=True  → 计费输入 = input - cache_read
    - cache_included=False → 计费输入 = input（缓存是另外记的，Anthropic 语义）
    """
    p = price_for(model)
    if p is None:
        return None
    m = 1_000_000.0
    cache_read_tokens = max(0, int(cache_read_tokens or 0))
    if cache_included:
        billable_input = max(0, int(input_tokens or 0) - cache_read_tokens)
    else:
        billable_input = int(input_tokens or 0)
    cny = (
        billable_input / m * p["input"]
        + cache_read_tokens / m * p["cache_read"]
        + int(output_tokens or 0) / m * p["output"]
    )
    return _to_cny(cny, p["currency"])


#: 这些源的 input_tokens **包含** cache_read（OpenAI 语义）
CACHE_INCLUSIVE_SOURCES = {"zcode", "codex"}


def estimate_row(row) -> float | None:
    """给 UsageRow 折算。"""
    src = str(getattr(row, "source", "") or "")
    return estimate(
        getattr(row, "model", "") or getattr(row, "label", ""),
        int(getattr(row, "input_tokens", 0) or 0),
        int(getattr(row, "output_tokens", 0) or 0),
        int(getattr(row, "cache_read_tokens", 0) or 0),
        cache_included=src in CACHE_INCLUSIVE_SOURCES,
    )


def summarize_cost(rows) -> dict:
    """汇总一批消耗行的等效成本。

    返回：
        {
          "currency": "CNY",
          "total": 12.34,               # 有单价的行加起来
          "priced_tokens": 123,         # 算到钱的那部分 token
          "unpriced_tokens": 456,       # 认不出模型、没算钱的那部分
          "unpriced_models": ["xxx"],   # 哪些模型没单价
          "by_model": [{"model","cost","tokens"}],
        }
    """
    by_model: dict[str, dict] = {}
    total = 0.0
    priced_tokens = 0
    unpriced_tokens = 0
    unpriced_models: set[str] = set()

    for r in rows or []:
        if getattr(r, "kind", "") != "consumption":
            continue
        tokens = int(getattr(r, "total_tokens", 0) or 0)
        model = str(getattr(r, "model", "") or "")
        cost = estimate_row(r)
        if cost is None:
            unpriced_tokens += tokens
            if model:
                unpriced_models.add(model)
            continue
        priced_tokens += tokens
        total += cost
        slot = by_model.setdefault(model, {"model": model, "cost": 0.0, "tokens": 0})
        slot["cost"] += cost
        slot["tokens"] += tokens

    items = sorted(by_model.values(), key=lambda x: -x["cost"])
    for it in items:
        it["cost"] = round(it["cost"], 4)

    return {
        "currency": "CNY",
        "total": round(total, 2),
        "priced_tokens": priced_tokens,
        "unpriced_tokens": unpriced_tokens,
        "unpriced_models": sorted(unpriced_models),
        "by_model": items,
    }
