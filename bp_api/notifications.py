"""Persistent outbound notifications and Feishu app-bot delivery."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg
import requests
from psycopg.types.json import Json

from . import cache, db

logger = logging.getLogger(__name__)

_RETRY_DELAYS_SECONDS = (60, 300, 900, 3600, 10800)
_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
_AUTH_ERROR_CODES = {99991663, 99991664, 99991668}
_TOKEN_EARLY_EXPIRY_SECONDS = 300
_token_lock = threading.Lock()
_local_tokens: dict[str, dict[str, Any]] = {}


@dataclass(frozen=True)
class FeishuConfig:
    app_id: str
    app_secret: str
    chat_id: str
    site_url: str
    max_attempts: int
    timeout_seconds: float

    @property
    def enabled(self) -> bool:
        return bool(self.app_id and self.app_secret and self.chat_id)


def load_feishu_config() -> FeishuConfig:
    try:
        max_attempts = max(1, int(os.getenv("BP_FEISHU_NOTIFY_MAX_ATTEMPTS", "5") or "5"))
    except ValueError:
        max_attempts = 5
    try:
        timeout_seconds = max(1.0, float(os.getenv("BP_FEISHU_NOTIFY_TIMEOUT_SECONDS", "10") or "10"))
    except ValueError:
        timeout_seconds = 10.0
    return FeishuConfig(
        app_id=os.getenv("BP_FEISHU_APP_ID", "").strip(),
        app_secret=os.getenv("BP_FEISHU_APP_SECRET", "").strip(),
        chat_id=os.getenv("BP_FEISHU_CHAT_ID", "").strip(),
        site_url=os.getenv("BP_SITE_URL", "").strip().rstrip("/"),
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
    )


def _is_daily_update(conn: psycopg.Connection, task_id: str | None) -> bool:
    if not task_id:
        return False
    with conn.cursor() as cur:
        cur.execute("SELECT task_type FROM bp_task WHERE task_id=%s", (task_id,))
        row = cur.fetchone()
    return bool(row and row[0] == "daily_update")


def enqueue_rebalance_event(
    conn: psycopg.Connection,
    *,
    task_id: str | None,
    portfolio_id: int,
    portfolio_name: str,
    method: str,
    method_name: str,
    trade_date: date,
    reason: str,
    rebalance_band: float,
    max_deviation: float | None,
    target_weights: dict[str, float],
    prev_weights: dict[str, float],
    delta: dict[str, float],
    asset_names: dict[str, str],
) -> int | None:
    """Insert one idempotent outbox event for a completed daily update."""
    if not load_feishu_config().enabled or not _is_daily_update(conn, task_id):
        return None
    if reason == "建仓":
        return None

    assets = []
    keys = set(target_weights) | set(prev_weights) | set(delta)
    for key in sorted(keys, key=lambda item: abs(float(delta.get(item, 0.0))), reverse=True):
        assets.append(
            {
                "key": key,
                "name": asset_names.get(key, key),
                "previous": float(prev_weights.get(key, 0.0)),
                "target": float(target_weights.get(key, 0.0)),
                "delta": float(delta.get(key, 0.0)),
            }
        )
    payload = {
        "portfolio_id": portfolio_id,
        "portfolio_name": portfolio_name,
        "method": method,
        "method_name": method_name,
        "trade_date": trade_date.isoformat(),
        "reason": reason,
        "rebalance_band": float(rebalance_band),
        "max_deviation": None if max_deviation is None else float(max_deviation),
        "assets": assets,
    }
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO bp_notification_outbox
                 (channel, event_type, portfolio_id, method, trade_date, payload)
               VALUES ('feishu', 'rebalance', %s, %s, %s, %s)
               ON CONFLICT (channel, event_type, portfolio_id, method, trade_date)
               DO NOTHING
               RETURNING notification_id""",
            (portfolio_id, method, trade_date, Json(payload)),
        )
        row = cur.fetchone()
    return int(row[0]) if row else None


def _escape_lark_md(value: Any) -> str:
    text = str(value)
    for char in ("\\", "*", "_", "~", "`", "[", "]"):
        text = text.replace(char, f"\\{char}")
    return text


def build_feishu_card(payload: dict, config: FeishuConfig) -> dict:
    portfolio_name = _escape_lark_md(payload["portfolio_name"])
    method_name = _escape_lark_md(payload["method_name"])
    reason = _escape_lark_md(payload["reason"])
    max_deviation = payload.get("max_deviation")
    deviation_text = "-" if max_deviation is None else f"{float(max_deviation) * 100:.2f}pp"
    summary = (
        f"**组合：** {portfolio_name}\n"
        f"**策略：** {method_name}\n"
        f"**数据日期：** {payload['trade_date']}\n"
        f"**触发原因：** {reason}\n"
        f"**最大偏离：** {deviation_text}　**偏离带：** {float(payload['rebalance_band']) * 100:.2f}pp"
    )
    asset_lines = ["**资产权重变动（调前 → 目标｜增减）**"]
    assets = payload.get("assets", [])
    for index, asset in enumerate(assets):
        line = (
            f"• {_escape_lark_md(asset['name'])}："
            f"{float(asset['previous']) * 100:.2f}% → {float(asset['target']) * 100:.2f}%｜"
            f"{float(asset['delta']) * 100:+.2f}pp"
        )
        candidate = "\n".join([*asset_lines, line])
        if len(candidate.encode("utf-8")) > 20_000:
            asset_lines.append(f"…其余 {len(assets) - index} 项请在组合详情中查看")
            break
        asset_lines.append(line)

    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": summary}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(asset_lines)}},
    ]
    if config.site_url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看组合详情"},
                        "url": f"{config.site_url}/dashboard?id={int(payload['portfolio_id'])}",
                        "type": "primary",
                    }
                ],
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": f"调仓提醒 · {payload['portfolio_name']}"},
        },
        "elements": elements,
    }


class FeishuApiError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code

    @property
    def is_auth_error(self) -> bool:
        return self.status_code == 401 or self.code in _AUTH_ERROR_CODES


def _response_json(response: requests.Response, operation: str) -> dict:
    try:
        result = response.json()
    except ValueError as exc:
        raise FeishuApiError(f"飞书{operation}返回了非 JSON 响应", status_code=response.status_code) from exc
    code = result.get("code")
    if response.status_code >= 400 or code != 0:
        message = result.get("msg", "unknown error")
        raise FeishuApiError(
            f"飞书{operation}失败 code={code}: {message}",
            code=code if isinstance(code, int) else None,
            status_code=response.status_code,
        )
    return result


def _token_cache_key(app_id: str) -> str:
    digest = hashlib.sha256(app_id.encode("utf-8")).hexdigest()[:16]
    return f"feishu:tenant_access_token:{digest}"


def _valid_cached_token(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    token = value.get("token")
    expires_at = value.get("expires_at")
    if isinstance(token, str) and token and isinstance(expires_at, (int, float)):
        if float(expires_at) > time.time():
            return token
    return None


def invalidate_tenant_access_token(config: FeishuConfig) -> None:
    key = _token_cache_key(config.app_id)
    _local_tokens.pop(key, None)
    cache.delete(key)


def get_tenant_access_token(config: FeishuConfig, *, force_refresh: bool = False) -> str:
    if not config.enabled:
        raise RuntimeError("飞书应用机器人配置不完整")
    key = _token_cache_key(config.app_id)
    if force_refresh:
        invalidate_tenant_access_token(config)
    else:
        token = _valid_cached_token(_local_tokens.get(key))
        if token:
            return token
        cached = cache.get_json(key)
        token = _valid_cached_token(cached)
        if token:
            _local_tokens[key] = cached
            return token

    with _token_lock:
        if not force_refresh:
            token = _valid_cached_token(_local_tokens.get(key))
            if token:
                return token
            cached = cache.get_json(key)
            token = _valid_cached_token(cached)
            if token:
                _local_tokens[key] = cached
                return token
        response = requests.post(
            _TOKEN_URL,
            json={"app_id": config.app_id, "app_secret": config.app_secret},
            timeout=config.timeout_seconds,
        )
        result = _response_json(response, "获取 tenant_access_token")
        token = result.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise FeishuApiError("飞书获取 tenant_access_token 响应缺少令牌")
        try:
            expires_in = max(1, int(result.get("expire", 7200)))
        except (TypeError, ValueError):
            expires_in = 7200
        safety_margin = min(_TOKEN_EARLY_EXPIRY_SECONDS, max(1, expires_in // 10))
        ttl = max(1, expires_in - safety_margin)
        cached_value = {"token": token, "expires_at": time.time() + ttl}
        _local_tokens[key] = cached_value
        cache.set_json(key, cached_value, ttl_seconds=ttl)
        return token


def _send_feishu_once(payload: dict, config: FeishuConfig, token: str) -> None:
    response = requests.post(
        _MESSAGE_URL,
        params={"receive_id_type": "chat_id"},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        json={
            "receive_id": config.chat_id,
            "msg_type": "interactive",
            "content": json.dumps(build_feishu_card(payload, config), ensure_ascii=False),
        },
        timeout=config.timeout_seconds,
    )
    _response_json(response, "发送消息")


def send_feishu(payload: dict, config: FeishuConfig | None = None) -> None:
    cfg = config or load_feishu_config()
    if not cfg.enabled:
        raise RuntimeError("飞书应用机器人配置不完整")
    token = get_tenant_access_token(cfg)
    try:
        _send_feishu_once(payload, cfg, token)
    except FeishuApiError as exc:
        if not exc.is_auth_error:
            raise
        token = get_tenant_access_token(cfg, force_refresh=True)
        _send_feishu_once(payload, cfg, token)


def _claim_one(config: FeishuConfig, notification_id: int | None = None) -> dict | None:
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            # A worker may die after claiming its final allowed attempt. Retire
            # that stale lease instead of leaving it in "sending" forever.
            cur.execute(
                """UPDATE bp_notification_outbox
                   SET status='exhausted', locked_at=NULL,
                       last_error=COALESCE(last_error, '发送进程在最终尝试中断'), updated_at=now()
                   WHERE channel='feishu' AND status='sending'
                     AND attempts >= %s
                     AND locked_at < now() - interval '10 minutes'""",
                (config.max_attempts,),
            )
            cur.execute(
                """WITH candidate AS (
                       SELECT notification_id
                       FROM bp_notification_outbox
                       WHERE channel='feishu'
                         AND attempts < %s
                         AND next_attempt_at <= now()
                         AND (status IN ('pending', 'failed')
                              OR (status='sending' AND locked_at < now() - interval '10 minutes'))
                         AND (%s::bigint IS NULL OR notification_id=%s)
                       ORDER BY created_at, notification_id
                       FOR UPDATE SKIP LOCKED
                       LIMIT 1
                   )
                   UPDATE bp_notification_outbox AS n
                   SET status='sending', attempts=n.attempts+1, locked_at=now(), updated_at=now()
                   FROM candidate
                   WHERE n.notification_id=candidate.notification_id
                   RETURNING n.notification_id, n.payload, n.attempts""",
                (config.max_attempts, notification_id, notification_id),
            )
            row = cur.fetchone()
        conn.commit()
    if not row:
        return None
    return {"notification_id": int(row[0]), "payload": row[1], "attempts": int(row[2])}


def _mark_sent(notification_id: int) -> None:
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE bp_notification_outbox
                   SET status='sent', sent_at=now(), locked_at=NULL, last_error=NULL, updated_at=now()
                   WHERE notification_id=%s""",
                (notification_id,),
            )
        conn.commit()


def _mark_failed(notification_id: int, attempts: int, max_attempts: int, error: str) -> None:
    exhausted = attempts >= max_attempts
    delay_index = min(max(attempts - 1, 0), len(_RETRY_DELAYS_SECONDS) - 1)
    next_attempt = datetime.now(timezone.utc) + timedelta(seconds=_RETRY_DELAYS_SECONDS[delay_index])
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE bp_notification_outbox
                   SET status=%s, next_attempt_at=%s, locked_at=NULL,
                       last_error=%s, updated_at=now()
                   WHERE notification_id=%s""",
                ("exhausted" if exhausted else "failed", next_attempt, error[:2000], notification_id),
            )
        conn.commit()


def dispatch_pending(notification_id: int | None = None, limit: int = 20) -> dict:
    """Deliver due outbox rows. Failures are persisted and never fail a backtest."""
    config = load_feishu_config()
    if not config.enabled:
        return {"sent": 0, "failed": 0, "disabled": True}
    sent = failed = 0
    for _ in range(max(1, limit)):
        claimed = _claim_one(config, notification_id)
        if not claimed:
            break
        current_id = claimed["notification_id"]
        try:
            send_feishu(claimed["payload"], config)
            _mark_sent(current_id)
            sent += 1
        except Exception as exc:  # noqa: BLE001 - delivery must not affect portfolio results
            failed += 1
            _mark_failed(current_id, claimed["attempts"], config.max_attempts, str(exc))
            logger.warning("飞书通知发送失败 notification_id=%s: %s", current_id, exc)
        if notification_id is not None:
            break
    return {"sent": sent, "failed": failed, "disabled": False}
