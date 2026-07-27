"""加密货币看板 — API endpoints (只读预计算表)。

公开端点 (无需登录):
  GET /api/crypto/correlation  — 全预计算相关性数据 (从 DB 预计算表组装, 不再计算)

数据流 (镜像 CFFEX 看板):
  bp_ingest 钩子 / 每日定时任务 → bp_ingest.crypto_corr.compute_and_store_crypto_corr
    → bp_crypto_corr_daily (Option A: 一行=一日×一对×一方法, 4 窗口作列, ~27k 行)
    + bp_crypto_price_daily (6 资产 NYSE 对齐收盘, ~10k 行) + bp_crypto_meta (KV)
    → invalidate_crypto_cache() (Redis delete)
    → _ping_crypto_revalidate() (Next.js cacheTag 失效)
  本 API: 读 version → Redis (crypto:correlation:v{version}) → miss 则只读预计算表组装。
  请求路径永不计算。

bp_ingest 的导入放函数内 (lazy), 避免模块级导入触发 uvicorn reload loop (仿 cffex._lazy_cmap)。
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
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

# Option A 列名 → (前端窗口 key, 组内数组 key)
_WIN_COLS = [
    ("corr_3m", "3M", "c3"),
    ("corr_6m", "6M", "c6"),
    ("corr_9m", "9M", "c9"),
    ("corr_12m", "12M", "c12"),
]


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


def _read_dates_btc(conn) -> tuple[list[str], list[float | None]]:
    """从 bp_crypto_price_daily 读 BTC 序列 → (dates, btc_prices) (顶层共享, 前端 x 轴 + 右轴叠加)。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trade_date, close FROM bp_crypto_price_daily "
            "WHERE symbol = 'BTC-USD' ORDER BY trade_date"
        )
        rows = cur.fetchall()
    dates = [r[0].isoformat() for r in rows]
    btc = [round(float(r[1]), 4) if r[1] is not None else None for r in rows]
    return dates, btc


def _read_rolling(conn) -> dict:
    """读 27k corr 行 (Option A) → rolling[win][method][pair] = {label, correlation}。

    镜像 CFFEX history 端点从 bp_cffex_premium_daily 组装序列: 按 (pair_key, method) 分组,
    4 个窗口列各成一个 corr 数组 (按 trade_date 升序)。dates + btc_prices 共享于顶层。
    """
    from bp_ingest.crypto_corr import ASSET_PAIRS

    with conn.cursor() as cur:
        cur.execute(
            "SELECT trade_date, pair_key, method, corr_3m, corr_6m, corr_9m, corr_12m "
            "FROM bp_crypto_corr_daily ORDER BY pair_key, method, trade_date"
        )
        rows = cur.fetchall()

    groups: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"c3": [], "c6": [], "c9": [], "c12": []}
    )
    for _td, pk, method, c3, c6, c9, c12 in rows:
        g = groups[(pk, method)]
        g["c3"].append(None if c3 is None else float(c3))
        g["c6"].append(None if c6 is None else float(c6))
        g["c9"].append(None if c9 is None else float(c9))
        g["c12"].append(None if c12 is None else float(c12))

    rolling: dict[str, dict[str, dict[str, dict]]] = {}
    for (pk, method), g in groups.items():
        label = ASSET_PAIRS.get(pk, {}).get("label", pk)
        for _col, win, arr_key in _WIN_COLS:
            rolling.setdefault(win, {}).setdefault(method, {})[pk] = {
                "label": label,
                "correlation": g[arr_key],
            }
    return rolling


def _read_snapshot(conn, effective_td: date) -> dict:
    """6 资产在 effective_td 的收盘 (从 bp_crypto_price_daily, 不碰 hypertable)。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, close FROM bp_crypto_price_daily WHERE trade_date = %s",
            (effective_td,),
        )
        by_sym = {r[0]: float(r[1]) for r in cur.fetchall()}
    snap: dict[str, Any] = {}
    for sym, field in _SYMBOL_TO_FIELD.items():
        v = by_sym.get(sym)
        snap[field] = round(v, 2) if v is not None else None
    return snap


def _read_lagged_shifted(conn) -> dict:
    """BTC[t] vs DXY[t+N] 滞后平移 (从 bp_crypto_price_daily 读 BTC+DXY 序列, API 内切片)。"""
    from bp_ingest.crypto_corr import LAG_PERIODS

    with conn.cursor() as cur:
        cur.execute(
            "SELECT trade_date, close FROM bp_crypto_price_daily "
            "WHERE symbol = 'BTC-USD' ORDER BY trade_date"
        )
        btc = {r[0]: float(r[1]) for r in cur.fetchall()}
        cur.execute(
            "SELECT trade_date, close FROM bp_crypto_price_daily "
            "WHERE symbol = 'DX-Y.NYB' ORDER BY trade_date"
        )
        dxy = {r[0]: float(r[1]) for r in cur.fetchall()}

    common = sorted(d for d in btc.keys() if d in dxy)
    n = len(common)
    lagged: dict[str, dict] = {}
    for lag_label, lag_days in LAG_PERIODS.items():
        if n <= lag_days:
            lagged[lag_label] = {"dates": [], "btc": [], "dxy": []}
            continue
        btc_slice = common[: n - lag_days]
        dxy_slice = common[lag_days:]
        lagged[lag_label] = {
            "dates": [d.isoformat() for d in btc_slice],
            "btc": [round(btc[d], 2) for d in btc_slice],
            "dxy": [round(dxy[dxy_slice[i]], 2) for i, d in enumerate(btc_slice)],
        }
    return lagged


def _fmt_et(ts: datetime) -> str:
    return ts.astimezone(NY_TZ).strftime("%Y-%m-%d %H:%M") + " ET"


def _fmt_cn(ts: datetime) -> str:
    return ts.astimezone(CST).strftime("%Y-%m-%d %H:%M") + " 北京时间"


def _build_payload() -> dict:
    """从 bp_crypto_meta + bp_crypto_corr_daily (Option A) + bp_crypto_price_daily 组装前端 payload。"""
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
        effective_td = date.fromisoformat(meta_kv["effective_td"])
        as_of_ts = datetime.fromisoformat(meta_kv["latest_as_of"])
        dates, btc_prices = _read_dates_btc(conn)
        snapshot = _read_snapshot(conn, effective_td)
        rolling = _read_rolling(conn)
        lagged = _read_lagged_shifted(conn)

    snapshot["as_of"] = as_of_ts.isoformat()
    version: int = 0
    try:
        version = int(meta_kv.get("version", "0") or "0")
    except (ValueError, TypeError):
        version = 0

    return {
        "is_ready": True,
        "dates": dates,            # 共享 NYSE 日期轴 (x 轴, 所有 series 共用)
        "btc_prices": btc_prices,  # 共享 BTC 收盘 (右轴叠加)
        "snapshot": _sanitize(snapshot),
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
