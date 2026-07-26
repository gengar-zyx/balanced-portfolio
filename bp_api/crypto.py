"""加密货币看板 — API endpoints。

公开端点 (无需登录):
  GET /api/crypto/correlation  — 全预计算相关性数据

缓存策略:
  - 主缓存: 进程内模块级变量 (_CACHED_DATA), 所有模式均可用
  - 辅助缓存: Redis (跨进程共享 + 持久化), 仅 REDIS_URL 可用时生效
  - API 启动时后台预热内存缓存 (27s → 首次请求 <50ms)
  - bp_ingest 入库后调用 invalidate_crypto_cache() 清空所有缓存
"""

from __future__ import annotations

import logging
import math
import threading

from fastapi import FastAPI, HTTPException

from . import cache, db

logger = logging.getLogger(__name__)

CACHE_KEY = "crypto:correlation:v2"
CACHE_TTL = 3600

# 进程内缓存 (所有模式均可用, 不依赖 Redis)
_CACHED_DATA: dict | None = None
_lock = threading.Lock()


def _sanitize(obj):
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
    return obj


def invalidate_crypto_cache() -> None:
    """入库后清空所有缓存 (内存 + Redis)。"""
    global _CACHED_DATA
    with _lock:
        _CACHED_DATA = None
    cache.delete(CACHE_KEY)
    cache.delete_pattern("crypto:correlation:*")
    logger.info("crypto 缓存已失效")


def _compute() -> dict:
    """全预计算 (不依赖任何缓存层)。"""
    from .quant.crypto_corr import build_all_correlations
    with db.get_conn() as conn:
        data = build_all_correlations(conn)
    return _sanitize(data)


def prewarm_crypto_cache() -> None:
    """启动时后台预热内存缓存 (非阻塞)。"""
    def _run():
        global _CACHED_DATA
        try:
            data = _compute()
            with _lock:
                _CACHED_DATA = data
            # best-effort 写 Redis (跨进程共享)
            cache.set_json(CACHE_KEY, data, ttl_seconds=CACHE_TTL)
            logger.info("crypto 缓存预热完成 (内存 + Redis)")
        except Exception as exc:
            logger.error("crypto 缓存预热失败: %s", exc, exc_info=True)

    t = threading.Thread(target=_run, daemon=True, name="crypto-prewarm")
    t.start()
    logger.info("crypto 缓存后台预热已启动")


def _get_cached() -> dict | None:
    """读取缓存: 内存优先 → Redis 兜底。"""
    global _CACHED_DATA
    with _lock:
        if _CACHED_DATA is not None:
            return _CACHED_DATA
    # 内存 miss → 试 Redis (跨进程 / 跨重启)
    return cache.get_json(CACHE_KEY)


def _set_cached(data: dict) -> None:
    """写入所有缓存层。"""
    global _CACHED_DATA
    with _lock:
        _CACHED_DATA = data
    cache.set_json(CACHE_KEY, data, ttl_seconds=CACHE_TTL)


def register_routes(app: FastAPI) -> None:

    @app.get("/api/crypto/correlation")
    def crypto_correlation() -> dict:
        try:
            # 1. 内存缓存 (ms 级, 所有模式可用)
            data = _get_cached()
            if data is not None:
                return data
            # 2. 首次计算 (28s, 仅启动后首次请求)
            logger.info("crypto 缓存未命中, 全量计算中 (约 27s)...")
            data = _compute()
            _set_cached(data)
            return data
        except Exception as exc:
            logger.error("crypto/correlation 异常: %s", exc, exc_info=True)
            raise HTTPException(500, f"获取相关性数据失败: {exc}")
