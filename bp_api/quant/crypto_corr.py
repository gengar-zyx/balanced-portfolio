"""加密货币相关性计算模块 — 全预计算引擎。

从 bp_quote_clean 读取 close 价格, 计算对数日收益率, 然后预计算所有组合:
  - 4 种方法: pearson / spearman / kendall / hoeffding
  - 4 个窗口: 3M / 6M / 9M / 12M (≈ 63 / 126 / 189 / 252 交易日)
  - 4 组资产对: COMEX黄金 / 沪金AU0 / 标普500 / 纳斯达克100
  - DXY 滞后平移价格序列 (BTC[t] vs DXY[t+N])

日历对齐: 以标普500在 bp_quote_clean 中的交易日期作为 NYSE 交易日历参考,
所有计算对齐到美股交易日, 确保相关性不会被非交易日零收益稀释。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Callable

import numpy as np
import pandas as pd
import psycopg
from scipy import stats as scipy_stats

logger = logging.getLogger(__name__)

# ---- 符号常量 ----
BTC_SYMBOL = "BTC-USD"
DXY_SYMBOL = "DX-Y.NYB"
COMEX_GOLD_SYMBOL = "GC=F"
SP500_SYMBOL = "标普500"

YFINANCE_SOURCE = "crypto_yfinance"
GLOBAL_INDEX_SOURCE = "global_index_em"
CMDTY_SOURCE = "cmdty_main_sina"

# 窗口: 月数 → 近似交易日数 (美股 ~252 日/年)
WINDOWS: dict[str, int] = {"3M": 63, "6M": 126, "9M": 189, "12M": 252}

METHODS = ["pearson", "spearman", "kendall", "hoeffding"]

ASSET_PAIRS: dict[str, dict[str, str]] = {
    "comex_gold": {"symbol": COMEX_GOLD_SYMBOL, "source": YFINANCE_SOURCE,      "label": "COMEX黄金"},
    "au0_gold":   {"symbol": "AU0",              "source": CMDTY_SOURCE,          "label": "沪金AU0"},
    "sp500":      {"symbol": SP500_SYMBOL,       "source": GLOBAL_INDEX_SOURCE,   "label": "标普500"},
    "nasdaq":     {"symbol": "纳斯达克",         "source": GLOBAL_INDEX_SOURCE,   "label": "纳斯达克100"},
}

LAG_PERIODS = WINDOWS


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------

def _load_close(
    conn: psycopg.Connection, symbol: str, source: str, start: date, end: date
) -> pd.Series:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT trade_date, close FROM bp_quote_clean
               WHERE symbol = %s AND source = %s
                 AND trade_date BETWEEN %s AND %s
               ORDER BY trade_date""",
            (symbol, source, start, end),
        )
        rows = cur.fetchall()
    if not rows:
        return pd.Series(dtype=float)
    s = pd.Series({r[0]: float(r[1]) for r in rows}, dtype=float)
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def _log_returns(close: pd.Series) -> pd.Series:
    return np.log(close / close.shift(1)).dropna()


def _nyse_calendar(
    conn: psycopg.Connection, start: date, end: date
) -> pd.DatetimeIndex:
    """以标普500在 bp_quote_clean 中的日期作为 NYSE 交易日历。"""
    sp500 = _load_close(conn, SP500_SYMBOL, GLOBAL_INDEX_SOURCE, start, end)
    return sp500.index.sort_values()


# ---------------------------------------------------------------------------
# 四种相关系数
# ---------------------------------------------------------------------------

def _pearson_r(x: np.ndarray, y: np.ndarray) -> float:
    return float(np.corrcoef(x, y)[0, 1])


def _spearman_r(x: np.ndarray, y: np.ndarray) -> float:
    from scipy.stats import rankdata
    return float(np.corrcoef(rankdata(x), rankdata(y))[0, 1])


def _kendall_tau(x: np.ndarray, y: np.ndarray) -> float:
    tau, _ = scipy_stats.kendalltau(x, y, variant="b")
    return float(tau) if not np.isnan(tau) else 0.0


def _hoeffding_d(x: np.ndarray, y: np.ndarray) -> float:
    n = len(x)
    if n < 10:
        return float("nan")
    from scipy.stats import rankdata
    rx = rankdata(x).astype(int)
    ry = rankdata(y).astype(int)
    ci = np.zeros(n, dtype=int)
    for i in range(n):
        ci[i] = int(np.sum((rx <= rx[i]) & (ry <= ry[i])))
    Q = np.sum((ci - 1) * (ci - 2))
    R = np.sum((rx - 1) * (rx - 2) * (ry - 1) * (ry - 2))
    T = np.sum((rx - 2) * (ry - 2) * (ci - 1))
    S = (n - 2) * (n - 3) * Q + R - 2 * (n - 2) * T
    denom = n * (n - 1) * (n - 2) * (n - 3) * (n - 4)
    if denom == 0:
        return float("nan")
    return float(30.0 * S / denom)


_CORR_FUNCS: dict[str, Callable[[np.ndarray, np.ndarray], float]] = {
    "pearson": _pearson_r,
    "spearman": _spearman_r,
    "kendall": _kendall_tau,
    "hoeffding": _hoeffding_d,
}


# ---------------------------------------------------------------------------
# 滚动相关系数 (按自然交易日, 不做 forward-fill)
# ---------------------------------------------------------------------------

def _rolling_corr(
    ret_a: pd.Series, ret_b: pd.Series, window: int, method: str,
    calendar: pd.DatetimeIndex,
) -> tuple[list[str], list[float | None]]:
    """在给定日历上计算滚动相关系数。结果对齐到 calendar。"""
    corr_func = _CORR_FUNCS[method]
    # 只取两个序列和日历三方都有数据的日期
    common = ret_a.index.intersection(ret_b.index).intersection(calendar).sort_values()
    if len(common) < window:
        return [], []

    ra = ret_a.loc[common].values
    rb = ret_b.loc[common].values
    n = len(ra)

    result: list[float | None] = [None] * (window - 1)
    for i in range(window - 1, n):
        x = ra[i - window + 1 : i + 1]
        y = rb[i - window + 1 : i + 1]
        if np.any(np.isnan(x)) or np.any(np.isnan(y)):
            result.append(None)
        else:
            try:
                r = corr_func(x, y)
                result.append(round(r, 4) if not np.isnan(r) else None)
            except Exception:
                result.append(None)

    # Serie 对齐到 calendar (不在 calendar 上的日期为 NaN)
    raw_s = pd.Series(result, index=common, dtype=object).reindex(calendar)
    dates_out = [d.strftime("%Y-%m-%d") for d in calendar]
    corr_out: list[float | None] = [
        None if pd.isna(v) else v for v in raw_s.tolist()
    ]
    return dates_out, corr_out


# ---------------------------------------------------------------------------
# 主入口: 全预计算
# ---------------------------------------------------------------------------

def build_all_correlations(conn: psycopg.Connection) -> dict[str, Any]:
    end = date.today()
    start = end - timedelta(days=2555)  # ~7 年 (足够最宽 252 日窗口)

    # ---- 加载所有 close ----
    btc_close = _load_close(conn, BTC_SYMBOL, YFINANCE_SOURCE, start, end)
    dxy_close = _load_close(conn, DXY_SYMBOL, YFINANCE_SOURCE, start, end)
    comex_close = _load_close(conn, COMEX_GOLD_SYMBOL, YFINANCE_SOURCE, start, end)
    au0_close = _load_close(conn, "AU0", CMDTY_SOURCE, start, end)

    # ---- NYSE 交易日历 (标普500 的交易日) ----
    nyse_cal = _nyse_calendar(conn, start, end)
    if len(nyse_cal) == 0:
        raise RuntimeError("标普500 无交易日数据, 无法获取 NYSE 日历")

    # BTC 在 NYSE 日的对数收益
    btc_nyse_close = btc_close.reindex(nyse_cal).dropna()
    btc_logret = _log_returns(btc_nyse_close)

    # ---- snapshot ----
    snapshot: dict[str, Any] = {
        "btc": round(float(btc_close.iloc[-1]), 2) if len(btc_close) > 0 else None,
        "dxy": round(float(dxy_close.iloc[-1]), 2) if len(dxy_close) > 0 else None,
        "comex_gold": round(float(comex_close.iloc[-1]), 2) if len(comex_close) > 0 else None,
        "au0_gold": round(float(au0_close.iloc[-1]), 2) if len(au0_close) > 0 else None,
        "as_of": end.isoformat(),
    }

    # 各资产在 NYSE 日的对数收益
    asset_close: dict[str, pd.Series] = {}
    asset_logrets: dict[str, pd.Series] = {}
    for key, info in ASSET_PAIRS.items():
        close_raw = _load_close(conn, info["symbol"], info["source"], start, end)
        close_nyse = close_raw.reindex(nyse_cal).dropna()
        asset_close[key] = close_nyse
        asset_logrets[key] = _log_returns(close_nyse) if len(close_nyse) > 0 else pd.Series(dtype=float)

    # ---- 预计算 rolling ----
    rolling: dict[str, dict[str, dict[str, Any]]] = {}
    for w_label, w_days in WINDOWS.items():
        rolling[w_label] = {}
        for method in METHODS:
            rolling[w_label][method] = {}
            for key, info in ASSET_PAIRS.items():
                ret_b = asset_logrets[key]
                if ret_b.empty or btc_logret.empty:
                    rolling[w_label][method][key] = {
                        "label": info["label"], "dates": [], "correlation": [], "btc_price": [],
                    }
                    continue
                dates_corr, corr_vals = _rolling_corr(
                    btc_logret, ret_b, w_days, method, nyse_cal,
                )
                # BTC close 对齐到 NYSE 日历
                btc_prices: list[float | None] = [
                    None if d not in btc_nyse_close.index or pd.isna(btc_nyse_close.loc[d])
                    else round(float(btc_nyse_close.loc[d]), 2)
                    for d in nyse_cal
                ]
                rolling[w_label][method][key] = {
                    "label": info["label"],
                    "dates": dates_corr,
                    "correlation": corr_vals,
                    "btc_price": btc_prices,
                }

    # ---- DXY 滞后平移价格 (Chart 2: BTC[t] vs DXY[t+N]) ----
    # 在 NYSE 日历上对齐 BTC 和 DXY
    dxy_nyse = dxy_close.reindex(nyse_cal).dropna()
    btc_nyse_aligned = btc_nyse_close.loc[dxy_nyse.index.intersection(btc_nyse_close.index)]
    common_idx = btc_nyse_aligned.index.intersection(dxy_nyse.index).sort_values()

    lagged_shifted: dict[str, dict[str, Any]] = {}
    for lag_label, lag_days in LAG_PERIODS.items():
        # 取 BTC[t] 和 DXY[t+N]
        n_common = len(common_idx)
        if n_common <= lag_days:
            lagged_shifted[lag_label] = {"dates": [], "btc": [], "dxy": []}
            continue
        btc_slice = common_idx[: n_common - lag_days]
        dxy_slice = common_idx[lag_days:]  # 向后平移 lag_days 个交易日
        lagged_shifted[lag_label] = {
            "dates": [d.strftime("%Y-%m-%d") for d in btc_slice],
            "btc": [
                round(float(btc_nyse_aligned.loc[d]), 2) for d in btc_slice
            ],
            "dxy": [
                round(float(dxy_nyse.loc[dxy_slice[i]]), 2)
                for i, d in enumerate(btc_slice)
            ],
        }

    # ---- meta ----
    meta = {
        "computed_at": end.isoformat(),
        "btc_data_end": (
            btc_close.index[-1].strftime("%Y-%m-%d") if len(btc_close) > 0 else None
        ),
        "window_sizes": {k: v for k, v in WINDOWS.items()},
        "methods": list(METHODS),
        "assets": {k: v["label"] for k, v in ASSET_PAIRS.items()},
        "calendar": "NYSE (标普500 交易日)",
    }

    return {
        "snapshot": snapshot,
        "rolling": rolling,
        "lagged_shifted": lagged_shifted,
        "meta": meta,
    }
