"""加密货币看板 — API endpoints (只读预计算表)。

公开端点 (无需登录):
  GET /api/crypto/correlation  — 全预计算相关性数据 (从 DB 预计算表组装, 不再计算)

数据流 (镜像 CFFEX 看板):
  bp_ingest 钩子 / 每日定时任务 → bp_ingest.crypto_corr.compute_and_store_crypto_corr
    → bp_crypto_corr_daily (64 series-per-row JSONB) + bp_crypto_meta (KV + 共享序列)
    → invalidate_crypto_cache() (Redis delete)
    → _ping_crypto_revalidate() (Next.js cacheTag 失效)
  本 API: 读 version → Redis (crypto:correlation:v{version}) → miss 则只读预计算表组装。
  请求路径永不计算; 64 行 JSONB + 1 行 meta → 远程 DB 读 <2s, Redis/Next.js 命中 <50ms/<100ms。

bp_ingest 的导入放函数内 (lazy), 避免模块级导入触发 uvicorn reload loop (仿 cffex._lazy_cmap)。
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException

from . import cache, db

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))
NY_TZ = ZoneInfo("America/New_York")

CACHE_KEY_PREFIX = "crypto:correlation"
CACHE_TTL = 3600

# symbol → 快照字段 (前端 Snapshot 契约)
_SYMBOL_TO_FIELD = {
    "BTC-USD": "btc",
    "DX-Y.NYB": "dxy",
    "GC=F": "comex_gold",
    "AU0": "au0_gold",
    "标普500": "sp500",
    "纳斯达克": "nasdaq",
}


def _sanitize(obj: Any) -> Any:
    """NaN/Inf → None (psycopg/JSON 拒绝 NaN/Infinity token)。"""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
    return obj


def invalidate_crypto_cache() -> None:
    """预计算落库后失效看板缓存 (delete_pattern 覆盖所有 version)。"""
    cache.delete_pattern(f"{CACHE_KEY_PREFIX}:*")
    logger.info("crypto 缓存已失效")


def _cache_key(version: Optional[int]) -> str:
    return f"{CACHE_KEY_PREFIX}:v{version}" if version is not None else f"{CACHE_KEY_PREFIX}:none"


def _read_version() -> Optional[int]:
    """读 bp_crypto_meta.version (单行, 廉价)。无 meta(冷启动) 返回 None。"""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM bp_crypto_meta WHERE key = 'version'")
            r = cur.fetchone()
    if not r or r[0] is None:
        return None
    try:
        return int(r[0])
    except (ValueError, TypeError):
        return None


def _read_meta(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM bp_crypto_meta")
        return {k: v for k, v in cur.fetchall()}


def _read_rolling(conn) -> dict:
    """组装 rolling[win][method][pair] = {label, correlation}。

    64 行 series-per-row; 日期轴 + BTC 价格共享于 payload 顶层 (避免 64× 冗余, payload 3.5MB→~700KB)。
    """
    from bp_ingest.crypto_corr import ASSET_PAIRS

    with conn.cursor() as cur:
        cur.execute("SELECT pair_key, win_label, method, correlations FROM bp_crypto_corr_daily")
        rows = cur.fetchall()

    rolling: dict[str, dict[str, dict[str, dict]]] = {}
    for pair_key, win_label, method, correlations in rows:
        corr = correlations if isinstance(correlations, list) else []
        rolling.setdefault(win_label, {}).setdefault(method, {})[pair_key] = {
            "label": ASSET_PAIRS.get(pair_key, {}).get("label", pair_key),
            "correlation": corr,
        }
    return rolling


def _read_lagged_shifted(nyse_dates: list[str], btc_prices: list, dxy_prices: list) -> dict:
    """BTC[t] vs DXY[t+N] 滞后平移价格 (纯函数, 从 meta 共享数组切片, 无 DB 查询)。"""
    from bp_ingest.crypto_corr import LAG_PERIODS

    n = len(nyse_dates)
    common_idx = [i for i in range(n) if btc_prices[i] is not None and dxy_prices[i] is not None]
    nc = len(common_idx)
    lagged: dict[str, dict] = {}
    for lag_label, lag_days in LAG_PERIODS.items():
        if nc <= lag_days:
            lagged[lag_label] = {"dates": [], "btc": [], "dxy": []}
            continue
        btc_idx = common_idx[: nc - lag_days]
        dxy_idx = common_idx[lag_days:]
        lagged[lag_label] = {
            "dates": [nyse_dates[i] for i in btc_idx],
            "btc": [round(btc_prices[i], 2) for i in btc_idx],
            "dxy": [round(dxy_prices[dxy_idx[j]], 2) for j in range(len(btc_idx))],
        }
    return lagged


def _fmt_et(ts: datetime) -> str:
    return ts.astimezone(NY_TZ).strftime("%Y-%m-%d %H:%M") + " ET"


def _fmt_cn(ts: datetime) -> str:
    return ts.astimezone(CST).strftime("%Y-%m-%d %H:%M") + " 北京时间"


def _build_payload() -> dict:
    """从 bp_crypto_meta (KV + 共享序列) + bp_crypto_corr_daily (64 series) 组装前端 payload。"""
    import json
    from bp_ingest.crypto_corr import ASSET_PAIRS, WINDOWS, METHODS

    with db.get_conn() as conn:
        meta_kv = _read_meta(conn)
        if not meta_kv or "effective_td" not in meta_kv or "latest_as_of" not in meta_kv:
            return {
                "is_ready": False,
                "snapshot": {},
                "rolling": {},
                "lagged_shifted": {},
                "meta": {"is_ready": False, "calendar": "NYSE (标普500 交易日)"},
            }
        nyse_dates = json.loads(meta_kv.get("nyse_dates") or "[]")
        btc_prices = json.loads(meta_kv.get("btc_prices") or "[]")
        dxy_prices = json.loads(meta_kv.get("dxy_prices") or "[]")
        snapshot_prices = json.loads(meta_kv.get("snapshot_prices") or "{}")
        effective_td = date.fromisoformat(meta_kv["effective_td"])
        as_of_ts = datetime.fromisoformat(meta_kv["latest_as_of"])
        rolling = _read_rolling(conn)

    lagged = _read_lagged_shifted(nyse_dates, btc_prices, dxy_prices)

    # 快照: 6 资产在 effective_td 的收盘 (来自 meta snapshot_prices)
    snap: dict[str, Any] = {}
    for sym, field in _SYMBOL_TO_FIELD.items():
        v = snapshot_prices.get(sym)
        snap[field] = round(v, 2) if v is not None else None
    snap["as_of"] = as_of_ts.isoformat()

    version: int = 0
    try:
        version = int(meta_kv.get("version", "0") or "0")
    except (ValueError, TypeError):
        version = 0

    return {
        "is_ready": True,
        "dates": nyse_dates,          # 共享 NYSE 日期轴 (x 轴, 所有 series 共用)
        "btc_prices": btc_prices,     # 共享 BTC 收盘 (右轴叠加, 所有 series 共用)
        "snapshot": _sanitize(snap),
        "rolling": _sanitize(rolling),
        "lagged_shifted": _sanitize(lagged),
        "meta": {
            "is_ready": True,
            "computed_at": meta_kv.get("computed_at"),
            "btc_data_end": meta_kv.get("btc_data_end"),
            "effective_td": meta_kv.get("effective_td"),
            "as_of": as_of_ts.isoformat(),
            "as_of_et": _fmt_et(as_of_ts),
            "as_of_cn": _fmt_cn(as_of_ts),
            "window_sizes": dict(WINDOWS),
            "methods": list(METHODS),
            "assets": {k: v["label"] for k, v in ASSET_PAIRS.items()},
            "calendar": "NYSE (标普500 交易日)",
            "version": version,
        },
    }


def register_routes(app: FastAPI) -> None:

    @app.get("/api/crypto/correlation")
    def crypto_correlation() -> dict:
        try:
            version = _read_version()
            key = _cache_key(version)
            cached = cache.get_json(key)
            if cached is not None:
                return cached
            payload = _build_payload()
            if payload.get("is_ready"):
                v = payload["meta"].get("version")
                cache.set_json(_cache_key(v), payload, ttl_seconds=CACHE_TTL)
            return payload
        except Exception as exc:
            logger.error("crypto/correlation 异常: %s", exc, exc_info=True)
            raise HTTPException(500, f"获取相关性数据失败: {exc}")
