"""官方数据源。

前面那些采集器读的是**本地日志**，口径是「我本地跑了多少」；
这一模块读的是**各家的官方接口**，口径是「账号还剩多少、什么时候重置」。

已实测可用的：
  ChatGPT / Codex → https://chatgpt.com/backend-api/wham/usage
      用 ~/.codex/auth.json 里的 OAuth access_token，
      返回 plan_type / used_percent / 窗口秒数 / reset_at。
      这就是 ChatGPT 设置页「用量」那一屏的同一份数据。
  DeepSeek         → https://api.deepseek.com/user/balance
      用 ~/.hermes/.env 里的 DEEPSEEK_API_KEY，返回账户余额。

每个适配器都遵守同一套规矩：
  - 拿不到凭据就跳过，不报错
  - 请求失败就返回空 + 原因，不影响其它源
  - 密钥只从本地文件读，绝不打印、绝不进日志、不进卡片
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from dotusage.util.config import xai_management_key
from dotusage.core.schema import SourceReport, UsageRow, now_ts, to_epoch

TIMEOUT = 20


def _http_json(url: str, headers: dict | None = None, timeout: int = TIMEOUT) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}") from e
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(str(e)[:120]) from e
    try:
        return json.loads(body)
    except Exception as e:
        raise RuntimeError("返回不是 JSON") from e


def _http_post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = TIMEOUT) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}") from e
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(str(e)[:120]) from e
    try:
        return json.loads(body)
    except Exception as e:
        raise RuntimeError("返回不是 JSON") from e


# --------------------------------------------------------------------------
# 凭据
# --------------------------------------------------------------------------
def codex_access_token() -> str:
    p = Path.home() / ".codex" / "auth.json"
    if not p.exists():
        return ""
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return str((d.get("tokens") or {}).get("access_token") or "")
    except Exception:
        return ""


def hermes_env() -> dict:
    """读 ~/.hermes/.env（只取需要的那几个键，不打印值）。"""
    p = Path.home() / ".hermes" / ".env"
    out: dict[str, str] = {}
    if not p.exists():
        return out
    try:
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------
# ChatGPT / Codex —— 官方配额 + 重置时间
# --------------------------------------------------------------------------
def chatgpt_usage() -> tuple[list[UsageRow], SourceReport]:
    rep = SourceReport("chatgpt")
    tok = codex_access_token()
    if not tok:
        rep.ok = False
        rep.error = "没有 ~/.codex/auth.json 里的 OAuth token"
        return [], rep

    try:
        data = _http_json(
            "https://chatgpt.com/backend-api/wham/usage",
            {"Authorization": f"Bearer {tok}", "Accept": "application/json"},
        )
    except Exception as e:  # noqa: BLE001
        rep.ok = False
        rep.error = f"查询失败 {e}"
        return [], rep

    rl = data.get("rate_limit") or {}
    rows: list[UsageRow] = []
    plan = data.get("plan_type") or ""

    for slot, label in (("primary_window", "ChatGPT"), ("secondary_window", "ChatGPT")):
        w = rl.get(slot)
        if not isinstance(w, dict):
            continue
        used = w.get("used_percent")
        if used is None:
            continue
        seconds = w.get("limit_window_seconds")
        reset_at = to_epoch(w.get("reset_at"))
        if not reset_at and w.get("reset_after_seconds"):
            reset_at = now_ts() + float(w["reset_after_seconds"])
        window = ""
        if seconds:
            if seconds % 86400 == 0:
                window = f"{int(seconds // 86400)}天窗口"
            elif seconds % 3600 == 0:
                window = f"{int(seconds // 3600)}小时窗口"
            else:
                window = f"{int(seconds // 60)}分钟窗口"
        rows.append(
            UsageRow(
                source="chatgpt",
                label=label,
                model=str(plan).upper(),
                kind="quota",
                used_percent=float(used),
                window=window,
                resets_at=reset_at or None,
                updated_at=now_ts(),
                note=("已达上限" if rl.get("limit_reached") else ""),
                path="chatgpt.com/backend-api/wham/usage",
            )
        )

    credits = data.get("credits") or {}
    bal = credits.get("balance") if isinstance(credits, dict) else None
    if isinstance(credits, dict) and (credits.get("unlimited") or (isinstance(bal, (int, float)) and bal > 0)):
        rows.append(
            UsageRow(
                source="chatgpt",
                label="ChatGPT 额度",
                model=str(plan).upper(),
                kind="balance",
                total_tokens=0,
                updated_at=now_ts(),
                headline_override="无限" if credits.get("unlimited") else f"${float(bal or 0):.2f}",
                detail_override="credits 余额",
                path="chatgpt.com/backend-api/wham/usage",
            )
        )

    rep.rows = len(rows)
    if not rows:
        rep.note = "接口未返回配额窗口"
    return rows, rep


# --------------------------------------------------------------------------
# DeepSeek —— 账户余额
# --------------------------------------------------------------------------
def deepseek_balance() -> tuple[list[UsageRow], SourceReport]:
    rep = SourceReport("deepseek")
    key = hermes_env().get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or ""
    if not key:
        rep.ok = False
        rep.error = "没有 DEEPSEEK_API_KEY"
        return [], rep

    try:
        data = _http_json(
            "https://api.deepseek.com/user/balance",
            {"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
    except Exception as e:  # noqa: BLE001
        rep.ok = False
        rep.error = f"查询失败 {e}"
        return [], rep

    rows: list[UsageRow] = []
    for b in data.get("balance_infos") or []:
        cur = str(b.get("currency") or "")
        total = float(b.get("total_balance") or 0)
        granted = float(b.get("granted_balance") or 0)
        topped = float(b.get("topped_up_balance") or 0)
        sym = {"CNY": "¥", "USD": "$"}.get(cur, cur + " ")
        rows.append(
            UsageRow(
                source="deepseek",
                label="DeepSeek 余额",
                model=cur,
                kind="balance",
                total_tokens=0,
                updated_at=now_ts(),
                headline_override=f"{sym}{total:.2f}",
                detail_override=("已充值" if topped else ""),
                path="api.deepseek.com/user/balance",
            )
        )

    rep.rows = len(rows)
    if not rows:
        rep.note = "接口未返回余额"
    return rows, rep


# --------------------------------------------------------------------------
# 候选密钥池
# --------------------------------------------------------------------------
def _candidate_keys(*names: str) -> list[str]:
    """收集候选 API Key。

    顺序：环境变量 → ZCode 的配置（它把各家 coding plan 的 key 都存着）→ Hermes 的 .env。
    真正的 key 不在这里面打印，只在内存里传。
    """
    env = hermes_env()
    out: list[str] = []
    for n in names:
        v = os.environ.get(n) or env.get(n)
        if v:
            out.append(v)

    # ZCode 的 provider_config.json / config.json 里存着各家的 key
    for path in (
        Path.home() / ".zcode" / "v2" / "config.json",
        Path.home() / ".zcode" / "v2" / "provider_config.json",
    ):
        if not path.exists():
            continue
        try:
            txt = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        import re

        for m in re.finditer(r'"apiKey"\s*:\s*"([^"]{20,200})"', txt):
            out.append(m.group(1))
    # 去重（保序）
    seen: set[str] = set()
    uniq: list[str] = []
    for k in out:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq


def _first_working(url: str, keys: list[str]) -> tuple[dict | None, str]:
    """挨个试密钥，返回第一个 HTTP 通了的结果。

    各家返回体长得不一样（z.ai 是 code/success，MiniMax 直接给 model_remains），
    所以这里只判断「请求成功且返回是 JSON 对象」，内容由调用方校验。
    """
    last = ""
    for k in keys:
        try:
            data = _http_json(url, {"Authorization": f"Bearer {k}", "Accept": "application/json"})
        except Exception as e:  # noqa: BLE001
            last = str(e)[:80]
            continue
        if isinstance(data, dict) and data:
            return data, k
        last = str(data)[:80]
    return None, last


# --------------------------------------------------------------------------
# z.ai / GLM Coding Plan —— 5 小时窗口 + 周窗口 + 工具额度
# --------------------------------------------------------------------------
ZAI_URLS = [
    "https://api.z.ai/api/monitor/usage/quota/limit",
    "https://open.bigmodel.cn/api/monitor/usage/quota/limit",
]

ZAI_WINDOW = {3: "5小时窗口", 6: "7天窗口"}


def zai_coding_plan() -> tuple[list[UsageRow], SourceReport]:
    rep = SourceReport("zai")
    keys = _candidate_keys("ZAI_API_KEY", "GLM_API_KEY")
    if not keys:
        rep.ok = False
        rep.error = "没找到 z.ai 的 API Key"
        return [], rep

    data = None
    err = ""
    for url in ZAI_URLS:
        cand, err = _first_working(url, keys)
        if cand and (cand.get("code") == 200 or cand.get("success") is True) and cand.get("data"):
            data = cand
            break
    if not data:
        rep.ok = False
        rep.error = f"查询失败 {err}"
        return [], rep

    payload = data.get("data") or {}
    limits = payload.get("limits") or []
    level = payload.get("level") or ""

    rows: list[UsageRow] = []
    for lim in limits:
        pct = lim.get("percentage")
        reset_ms = lim.get("nextResetTime")
        resets_at = float(reset_ms) / 1000 if reset_ms else None
        ltype = str(lim.get("type") or "")
        unit = lim.get("unit")

        if ltype == "TIME_LIMIT":
            used = lim.get("currentValue")
            total = lim.get("usage")
            rows.append(
                UsageRow(
                    source="zai",
                    label="Z.ai 工具额度",
                    model=str(level).upper(),
                    kind="balance",
                    total_tokens=0,
                    updated_at=now_ts(),
                    headline_override=f"{int(used or 0)}",
                    detail_override="月度",
                    path="api.z.ai/api/monitor/usage/quota/limit",
                )
            )
            continue

        if pct is None:
            continue
        window = ZAI_WINDOW.get(unit, "")
        rows.append(
            UsageRow(
                source="zai",
                label="Z.ai · GLM Coding Plan",
                model=str(level).upper(),
                kind="quota",
                used_percent=float(pct),
                window=window,
                resets_at=resets_at,
                updated_at=now_ts(),
                path="api.z.ai/api/monitor/usage/quota/limit",
            )
        )

    rep.rows = len(rows)
    if not rows:
        rep.note = "接口未返回窗口"
    return rows, rep


# --------------------------------------------------------------------------
# MiniMax M Plan / Coding Plan —— 5 小时窗口 + 周窗口
# --------------------------------------------------------------------------
MINIMAX_URLS = [
    "https://www.minimaxi.com/v1/api/openplatform/coding_plan/remains",
    "https://api.minimaxi.com/v1/api/openplatform/coding_plan/remains",
    "https://www.minimaxi.com/v1/token_plan/remains",
]


def minimax_usage() -> tuple[list[UsageRow], SourceReport]:
    rep = SourceReport("minimax")
    keys = _candidate_keys("MINIMAX_CN_API_KEY", "MINIMAX_PORTAL_API_KEY", "MINIMAX_API_KEY")
    if not keys:
        rep.ok = False
        rep.error = "没找到 MiniMax 的 API Key"
        return [], rep

    data = None
    err = ""
    for url in MINIMAX_URLS:
        data, err = _first_working(url, keys)
        if data and data.get("model_remains"):
            break
    if not data or not data.get("model_remains"):
        rep.ok = False
        rep.error = f"查询失败 {err}"
        return [], rep

    rows: list[UsageRow] = []
    for m in data.get("model_remains") or []:
        name = str(m.get("model_name") or "general")
        label = "MiniMax · M Plan" if name == "general" else f"MiniMax · {name}"

        interval_left = m.get("current_interval_remaining_percent")
        if interval_left is not None:
            rows.append(
                UsageRow(
                    source="minimax",
                    label=label,
                    model=name,
                    kind="quota",
                    used_percent=100.0 - float(interval_left),
                    window="5小时窗口",
                    resets_at=(now_ts() + float(m.get("remains_time") or 0) / 1000),
                    updated_at=now_ts(),
                    path="minimaxi.com/coding_plan/remains",
                )
            )

        weekly_left = m.get("current_weekly_remaining_percent")
        if weekly_left is not None:
            rows.append(
                UsageRow(
                    source="minimax",
                    label=label,
                    model=name,
                    kind="quota",
                    used_percent=100.0 - float(weekly_left),
                    window="7天窗口",
                    resets_at=(now_ts() + float(m.get("weekly_remains_time") or 0) / 1000),
                    updated_at=now_ts(),
                    path="minimaxi.com/coding_plan/remains",
                )
            )

    rep.rows = len(rows)
    if not rows:
        rep.note = "接口未返回窗口"
    return rows, rep


# --------------------------------------------------------------------------
# Grok —— xAI 订阅档位 / 账单
#
# 这一段踩过的坑，别再重复试了（2026-10-29 实测）：
#  - xAI 官方 OpenAPI 一共 39 个端点，**没有** usage / billing / credits 任何一个
#  - api.x.ai 响应头只有 x-ratelimit-limit/remaining-tokens|requests，
#    看着像配额，其实��**分钟级滚动窗口**：连打 5 次能看到 remaining-requests
#    从 120 掉到 118，隔几十秒又回到 120。当月度额度用是错的
#  - 真正的账单在 Management API（management-api.x.ai），但
#    **OAuth token 被拒**：{"message":"Action cannot be performed by OAuth2 token users"}
#    必须用 console.x.ai → Settings → Management Keys 单独建的 management key
#  - grok.com 的公开 REST 里能用的只有 /rest/subscriptions（拿订阅档位）
#
# 所以策略是：
#   有 management key → 真数据（周期消费 $、消费上限 $、预付余额）
#   没有             → 订阅档位（官方可查）+ 限额上限（官方头）+ 本机消耗
# --------------------------------------------------------------------------
HERMES_AUTH_FILE = Path.home() / ".hermes" / "auth.json"
XAI_MGMT_BASE = "https://management-api.x.ai/v1"

# grok.com 的边缘节点会拒 Python-urllib 的默认 UA（403），带个正常 UA 才通
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

TIER_SHORT = {
    "SUBSCRIPTION_TIER_X_PREMIUM_PLUS": "Premium+",
    "SUBSCRIPTION_TIER_X_PREMIUM": "Premium",
    "SUBSCRIPTION_TIER_X_BASIC": "Basic",
    "SUBSCRIPTION_TIER_FREE": "Free",
    "SUBSCRIPTION_TIER_SUPERGROK": "SuperGrok",
}


def _jwt_claims(tok: str) -> dict:
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        d = json.loads(base64.urlsafe_b64decode(p))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _jwt_claim(tok: str, name: str):
    """读 JWT payload 里的一个字段（xAI 的 team_id / tier 就在里面）。"""
    return _jwt_claims(tok).get(name)


XAI_TOKEN_CACHE = Path.home() / ".local" / "state" / "dot-usage" / "xai_token.json"


def _xai_refresh_token(refresh: str, client_id: str) -> dict:
    """用 refresh_token 换新的 access_token。"""
    body = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id}
    ).encode()
    req = urllib.request.Request(
        "https://auth.x.ai/oauth2/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": BROWSER_UA},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _xai_creds() -> tuple[str, str]:
    """取 xAI 的 OAuth token 和 team_id。

    token 只活 **6 小时**，一过期 grok.com / api.x.ai 全 401。
    所以做了三级兜底：
      1. 自己缓存的（可能是之前刷新过的）
      2. Hermes 凭据池里的（如果还没过期）
      3. 用 refresh_token 换新的，换到就写回自己的缓存

    不往 ~/.hermes/auth.json 写——那是 Hermes 自己的凭据库，它会自己管，
    我们抢着改容易和它打架。
    """
    now = time.time()

    # ---- 1. 自己的缓存 ----
    cached: dict = {}
    try:
        if XAI_TOKEN_CACHE.exists():
            cached = json.loads(XAI_TOKEN_CACHE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        cached = {}
    ctok = str(cached.get("access_token") or "")
    if ctok and float(_jwt_claims(ctok).get("exp") or 0) > now + 120:
        return ctok, str(cached.get("team_id") or _jwt_claim(ctok, "team_id") or "")

    # ---- 2. Hermes 凭据池 ----
    tok = ""
    refresh = str(cached.get("refresh_token") or "")
    client_id = ""
    team = ""
    if HERMES_AUTH_FILE.exists():
        try:
            d = json.loads(HERMES_AUTH_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            d = {}
        for key, prov in (d.get("providers") or {}).items():
            if not isinstance(prov, dict):
                continue
            if "xai" not in key.lower() and "xai" not in str(prov.get("name", "")).lower():
                continue
            t = prov.get("tokens") or {}
            ht = str(t.get("access_token") or "")
            if not ht:
                continue
            claims = _jwt_claims(ht)
            team = str(claims.get("team_id") or "")
            client_id = str(claims.get("client_id") or "")
            refresh = refresh or str(t.get("refresh_token") or "")
            if float(claims.get("exp") or 0) > now + 120:
                return ht, team
            tok = ht
            break

    # ---- 3. 刷新 ----
    if refresh and client_id:
        try:
            res = _xai_refresh_token(refresh, client_id)
            at = str(res.get("access_token") or "")
            if at:
                claims = _jwt_claims(at)
                try:
                    XAI_TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
                    XAI_TOKEN_CACHE.write_text(
                        json.dumps(
                            {
                                "access_token": at,
                                "refresh_token": res.get("refresh_token") or refresh,
                                "team_id": claims.get("team_id") or team,
                                "expires_at": claims.get("exp") or 0,
                                "refreshed_at": now,
                            },
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                except Exception:  # noqa: BLE001
                    pass
                return at, str(claims.get("team_id") or team)
        except Exception:  # noqa: BLE001
            pass

    return tok, team


def _grok_subscription(tok: str) -> tuple[str, str]:
    """查订阅档位。返回 (档位短名, 原始档位串)。"""
    try:
        d = _http_json(
            "https://grok.com/rest/subscriptions",
            {"Authorization": f"Bearer {tok}", "Accept": "application/json", "User-Agent": BROWSER_UA},
        )
    except Exception:  # noqa: BLE001
        return "", ""
    subs = d.get("subscriptions") or []
    if not subs:
        return "", ""
    raw = str(subs[0].get("tier") or "")
    plan = ((d.get("dominantPlan") or {}).get("plan") or "")
    short = TIER_SHORT.get(raw, "")
    if not short and plan:
        short = plan.replace("x.", "").replace("_", "+").title()
    return short, raw


def _grok_management(team_id: str, key: str) -> dict:
    """Management API 账单数据。返回 dict，拿不到就返回空 dict。"""
    h = {"Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": BROWSER_UA}
    out: dict = {}

    # 1) 本周期消费（美元）
    try:
        now = datetime.now()
        body = {
            "analyticsRequest": {
                "timeRange": {
                    "startTime": (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),
                    "endTime": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "timezone": "Asia/Shanghai",
                },
                "timeUnit": "TIME_UNIT_DAY",
                "values": [{"name": "usd", "aggregation": "AGGREGATION_SUM"}],
                "groupBy": [],
                "filters": [],
            }
        }
        d = _http_post_json(f"{XAI_MGMT_BASE}/billing/teams/{team_id}/usage", body, h)
        total = 0.0
        for ts in d.get("timeSeries") or []:
            for pt in ts.get("dataPoints") or []:
                for v in pt.get("values") or []:
                    total += float(v)
        out["spend_usd"] = total
    except Exception:  # noqa: BLE001
        pass

    # 2) 后付费消费上限（美元）
    try:
        d = _http_json(f"{XAI_MGMT_BASE}/billing/teams/{team_id}/postpaid/spending-limits", h)
        sl = d.get("spendingLimits") or {}
        val = (sl.get("effectiveHardSl") or {}).get("val")
        if val not in (None, ""):
            out["limit_usd"] = float(val) / 100.0
    except Exception:  # noqa: BLE001
        pass

    # 3) 预付余额（美元）
    try:
        d = _http_json(f"{XAI_MGMT_BASE}/billing/teams/{team_id}/prepaid/balance", h)
        tot = (d.get("total") or {}).get("val")
        if tot not in (None, ""):
            out["prepaid_usd"] = float(tot) / 100.0
    except Exception:  # noqa: BLE001
        pass

    return out


def _grok_local_spend() -> tuple[int, float]:
    """本机经 xAI 走过的量：Hermes 计费表里的 xai 行。返回 (token, 美元)。"""
    p = Path.home() / ".hermes" / "state.db"
    if not p.exists():
        return 0, 0.0
    try:
        import sqlite3

        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        rows = c.execute(
            "select coalesce(sum(input_tokens),0)+coalesce(sum(output_tokens),0),"
            " coalesce(sum(actual_cost_usd),0) from session_model_usage"
            " where billing_provider like 'xai%'"
        ).fetchone()
        c.close()
        return int(rows[0] or 0), float(rows[1] or 0)
    except Exception:  # noqa: BLE001
        return 0, 0.0


def grok_usage() -> tuple[list[UsageRow], SourceReport]:
    """Grok（xAI）：订阅档位 + 管理密钥账单。"""
    rep = SourceReport("grok")
    tok, team_id = _xai_creds()
    if not tok:
        rep.ok = False
        rep.error = "没有 xAI 凭据（Hermes 凭据池里没有 xai-oauth）"
        return [], rep

    rows: list[UsageRow] = []
    notes: list[str] = []

    # ---- 有 management key → 真账单 ----
    mgmt_key = xai_management_key()
    mg: dict = {}
    if mgmt_key and team_id:
        mg = _grok_management(team_id, mgmt_key)
        if mg:
            spend = mg.get("spend_usd")
            limit = mg.get("limit_usd")
            pct = None
            if spend is not None and limit:
                pct = max(0.0, min(100.0, spend / limit * 100.0))
            rows.append(
                UsageRow(
                    source="grok",
                    label="Grok · xAI",
                    model="grok",
                    kind="quota",
                    used_percent=pct,
                    window="月度账单周期",
                    cost_usd=spend,
                    updated_at=now_ts(),
                    note=f"本期消费 ${spend:.2f}" if spend is not None else "",
                    path="management-api.x.ai/billing",
                )
            )
            if mg.get("prepaid_usd") is not None:
                rows.append(
                    UsageRow(
                        source="grok",
                        label="Grok 预付余额",
                        model="grok",
                        kind="balance",
                        updated_at=now_ts(),
                        headline_override=f"${mg['prepaid_usd']:.2f}",
                        detail_override="预付余额",
                        path="management-api.x.ai/billing",
                    )
                )
            notes.append("management key")

    # ---- 订阅档位（公开可查，不花钱）----
    short, raw_tier = _grok_subscription(tok)
    local_tokens, local_cost = _grok_local_spend()

    if not any(r.kind == "quota" for r in rows):
        # 没有管理密钥就没有消费百分比，只能显示订阅档位
        head = short or "已订阅"
        rows.append(
            UsageRow(
                source="grok",
                label="Grok · xAI",
                model=raw_tier or "grok",
                kind="quota",
                used_percent=None,
                window="",
                total_tokens=local_tokens,
                cost_usd=local_cost or None,
                updated_at=now_ts(),
                note="官方未开放用量接口",
                path="grok.com/rest/subscriptions",
                headline_override=head,
                detail_override="",
            )
        )

    rep.rows = len(rows)
    rep.ok = bool(rows)
    if short:
        rep.note = f"档位 {short}" + ("，含账单" if mg else "，官方无用量接口")
    else:
        rep.note = "官方未开放用量接口"
    if notes:
        rep.note += f"（{'/'.join(notes)}）"
    return rows, rep


OFFICIAL_COLLECTORS = [
    ("chatgpt", chatgpt_usage),
    ("deepseek", deepseek_balance),
    ("minimax", minimax_usage),
    ("grok", grok_usage),
    ("zai", zai_coding_plan),
]


PROVIDERS = dict(OFFICIAL_COLLECTORS)


def collect_official(names: list[str] | None = None) -> tuple[list[UsageRow], list[SourceReport]]:
    """跑官方配额 provider。names 给了就只跑那几个。"""
    rows: list[UsageRow] = []
    reports: list[SourceReport] = []
    for name, fn in OFFICIAL_COLLECTORS:
        if names is not None and name not in names:
            continue
        try:
            r, rep = fn()
        except Exception as e:  # noqa: BLE001
            r, rep = [], SourceReport(name, ok=False, error=f"异常 {e}")
        rows.extend(r)
        reports.append(rep)
    return rows, reports
