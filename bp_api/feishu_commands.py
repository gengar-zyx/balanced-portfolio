"""Feishu ``/position`` command ingestion, durable processing and replies."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import quote

import psycopg
import requests
from psycopg.types.json import Json

from . import db, feishu_cards, notifications, repositories as repo, tasking

logger = logging.getLogger(__name__)

_COMMAND_RE = re.compile(r"^/position(?:\s+(.*?))?\s*$", re.IGNORECASE | re.DOTALL)
_RETRY_DELAYS_SECONDS = (60, 300, 900, 3600, 10800)
_REPLY_URL = "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"


@dataclass(frozen=True)
class FeishuCommandConfig:
    enabled_flag: bool
    max_attempts: int

    @property
    def enabled(self) -> bool:
        return self.enabled_flag and notifications.load_feishu_config().enabled


@dataclass(frozen=True)
class IncomingCommand:
    event_id: str
    message_id: str
    chat_id: str
    chat_type: str
    sender_type: str
    message_type: str
    text: str
    mention_keys: tuple[str, ...] = ()


def load_command_config() -> FeishuCommandConfig:
    enabled = os.getenv("BP_FEISHU_COMMANDS_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }
    try:
        max_attempts = max(1, int(os.getenv("BP_FEISHU_COMMAND_MAX_ATTEMPTS", "5") or "5"))
    except ValueError:
        max_attempts = 5
    return FeishuCommandConfig(enabled_flag=enabled, max_attempts=max_attempts)


def normalize_command_text(text: str, mention_keys: Iterable[str] = ()) -> str:
    """Remove Feishu mention placeholders and normalize surrounding whitespace."""
    normalized = text
    for key in mention_keys:
        if key:
            normalized = normalized.replace(key, " ")
    return " ".join(normalized.split())


def parse_position_command(text: str) -> str | None:
    match = _COMMAND_RE.fullmatch(text.strip())
    if not match:
        return None
    argument = (match.group(1) or "").strip()
    return argument


def accept_incoming(command: IncomingCommand, config: FeishuCommandConfig | None = None) -> str | None:
    """Return the parsed argument when the event is an allowed /position command."""
    cfg = config or load_command_config()
    if not cfg.enabled or command.message_type != "text":
        return None
    if command.sender_type.lower() != "user":
        return None
    feishu = notifications.load_feishu_config()
    chat_type = command.chat_type.lower()
    if chat_type == "group":
        if command.chat_id != feishu.chat_id or not command.mention_keys:
            return None
    elif chat_type != "p2p":
        return None
    normalized = normalize_command_text(command.text, command.mention_keys)
    return parse_position_command(normalized)


def persist_command_event(conn: psycopg.Connection, command: IncomingCommand, argument: str) -> int | None:
    """Persist an accepted event once; raw event bodies and credentials are never stored."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO bp_feishu_command_event
                 (event_id, message_id, chat_id, chat_type, command, argument)
               VALUES (%s,%s,%s,%s,'position',%s)
               ON CONFLICT DO NOTHING
               RETURNING command_event_id""",
            (command.event_id, command.message_id, command.chat_id, command.chat_type, argument or None),
        )
        row = cur.fetchone()
    return int(row[0]) if row else None


def ingest_command(command: IncomingCommand) -> int | None:
    """Validate, persist, commit, then ask Celery to process an inbound event."""
    argument = accept_incoming(command)
    if argument is None:
        return None
    with db.get_conn() as conn:
        command_event_id = persist_command_event(conn, command, argument)
        conn.commit()
    if command_event_id is not None:
        tasking.enqueue_task(
            "bp_api.dispatch_feishu_commands",
            {"command_event_id": command_event_id, "limit": 1},
        )
    return command_event_id


def _error_payload(title: str, message: str) -> dict:
    return {"ok": False, "title": title, "message": message}


def query_position(conn: psycopg.Connection, argument: str | None) -> dict:
    """Read the current strategy's most recent rebalance target weights."""
    value = (argument or "").strip()
    if not value:
        portfolio_id = repo.get_demo_id(conn)
        if portfolio_id is None:
            return _error_payload("没有默认组合", "系统尚未配置公开 Demo 组合。")
    elif value.isdecimal():
        portfolio_id = int(value)
    else:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT portfolio_id, name FROM bp_portfolio
                   WHERE lower(name)=lower(%s)
                   ORDER BY portfolio_id""",
                (value,),
            )
            matches = cur.fetchall()
        if not matches:
            return _error_payload("组合不存在", f"未找到名称为“{value}”的组合。可改用 /position <组合ID>。")
        if len(matches) > 1:
            candidates = "、".join(f"{row[1]}（ID {row[0]}）" for row in matches)
            return _error_payload("组合名称重复", f"请改用组合 ID：{candidates}")
        portfolio_id = int(matches[0][0])

    with conn.cursor() as cur:
        cur.execute(
            """SELECT name, method, status, error, data_as_of_date
               FROM bp_portfolio WHERE portfolio_id=%s""",
            (portfolio_id,),
        )
        portfolio = cur.fetchone()
        if not portfolio:
            return _error_payload("组合不存在", f"未找到组合 ID {portfolio_id}。")
        name, method, status, error, data_as_of_date = portfolio
        if status != "done":
            if status == "error":
                detail = f"组合回测失败：{str(error)[:1000] if error else '未提供错误详情'}"
            elif status == "running":
                detail = "组合正在计算，请稍后重试。"
            else:
                detail = "组合尚未完成首次回测。"
            return _error_payload("暂无当前持仓", detail)

        cur.execute(
            """SELECT trade_date, target_weights
               FROM bp_backtest_rebalance
               WHERE portfolio_id=%s AND method=%s
               ORDER BY trade_date DESC LIMIT 1""",
            (portfolio_id, method),
        )
        rebalance = cur.fetchone()
        if not rebalance:
            return _error_payload("暂无当前持仓", "当前策略还没有调仓或建仓记录。")

        cur.execute(
            """SELECT symbol, source, max(display_name)
               FROM bp_portfolio_asset
               WHERE portfolio_id=%s
               GROUP BY symbol, source""",
            (portfolio_id,),
        )
        names = {f"{row[0]}@{row[1]}": (row[2] or row[0]) for row in cur.fetchall()}

    trade_date, weights = rebalance
    holdings = [
        {"key": key, "name": names.get(key, key), "weight": float(weight)}
        for key, weight in sorted((weights or {}).items(), key=lambda item: -float(item[1]))
    ]
    return {
        "ok": True,
        "portfolio_id": portfolio_id,
        "portfolio_name": name,
        "method": method,
        "method_name": repo.METHOD_LABELS_CN.get(method, method),
        "data_as_of_date": data_as_of_date.isoformat() if data_as_of_date else None,
        "holding_date": trade_date.isoformat() if hasattr(trade_date, "isoformat") else str(trade_date),
        "holdings": holdings,
        "total_weight": sum(item["weight"] for item in holdings),
    }


def _escape_lark_md(value: Any) -> str:
    return feishu_cards.escape_lark_md(value)


def build_position_card(payload: dict) -> dict:
    if not payload.get("ok"):
        return feishu_cards.build_card(
            title=str(payload.get("title", "查询失败")),
            template="red",
            elements=[
                {"tag": "div", "text": {"tag": "lark_md", "content": _escape_lark_md(payload.get("message", "未知错误"))}},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": "用法：/position [组合ID或完整名称]"}]},
            ],
        )

    summary = (
        f"**组合：** {_escape_lark_md(payload['portfolio_name'])}（ID {payload['portfolio_id']}）\n"
        f"**当前策略：** {_escape_lark_md(payload['method_name'])}\n"
        f"**结果数据日期：** {payload.get('data_as_of_date') or '-'}\n"
        f"**持仓生效日期：** {payload['holding_date']}"
    )
    holding_elements: list[dict] = []
    lines = ["**当前目标持仓**"]
    for item in payload.get("holdings", []):
        lines.append(f"• {_escape_lark_md(item['name'])}：{float(item['weight']) * 100:.2f}%")
        if len(lines) >= 41:
            holding_elements.append(
                {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}
            )
            lines = []
    lines.append(f"**合计：{float(payload.get('total_weight', 0)) * 100:.2f}%**")
    holding_elements.append(
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}
    )
    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": summary}},
    ]
    chart = feishu_cards.build_position_chart(payload.get("holdings", []))
    if chart is not None:
        elements.append(chart)
    elements.extend(({"tag": "hr"}, *holding_elements))
    return feishu_cards.build_card(
        title=f"当前持仓 · {payload['portfolio_name']}",
        template="blue",
        elements=elements,
    )


def _reply_once(message_id: str, card: dict, config: notifications.FeishuConfig, token: str) -> None:
    response = requests.post(
        _REPLY_URL.format(message_id=quote(message_id, safe="")),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        json={
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False, allow_nan=False),
            # Feishu deduplicates retries carrying the same UUID. This closes the
            # crash window between a successful HTTP reply and our DB status update.
            "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"bp-position:{message_id}")),
        },
        timeout=config.timeout_seconds,
    )
    notifications._response_json(response, "回复消息")  # noqa: SLF001


def reply_feishu(message_id: str, card: dict, config: notifications.FeishuConfig | None = None) -> None:
    cfg = config or notifications.load_feishu_config()
    if not cfg.enabled:
        raise RuntimeError("飞书应用机器人配置不完整")
    token = notifications.get_tenant_access_token(cfg)
    try:
        _reply_once(message_id, card, cfg, token)
    except notifications.FeishuApiError as exc:
        if not exc.is_auth_error:
            raise
        token = notifications.get_tenant_access_token(cfg, force_refresh=True)
        _reply_once(message_id, card, cfg, token)


def _claim_one(max_attempts: int, command_event_id: int | None = None) -> dict | None:
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE bp_feishu_command_event
                   SET status='exhausted', locked_at=NULL,
                       last_error=COALESCE(last_error, '回复进程在最终尝试中断'), updated_at=now()
                   WHERE status='sending' AND attempts >= %s
                     AND locked_at < now() - interval '10 minutes'""",
                (max_attempts,),
            )
            cur.execute(
                """WITH candidate AS (
                       SELECT command_event_id FROM bp_feishu_command_event
                       WHERE attempts < %s AND next_attempt_at <= now()
                         AND (status IN ('pending','failed')
                              OR (status='sending' AND locked_at < now() - interval '10 minutes'))
                         AND (%s::bigint IS NULL OR command_event_id=%s)
                       ORDER BY created_at, command_event_id
                       FOR UPDATE SKIP LOCKED LIMIT 1
                   )
                   UPDATE bp_feishu_command_event AS e
                   SET status='sending', attempts=e.attempts+1, locked_at=now(), updated_at=now()
                   FROM candidate WHERE e.command_event_id=candidate.command_event_id
                   RETURNING e.command_event_id, e.message_id, e.argument, e.attempts""",
                (max_attempts, command_event_id, command_event_id),
            )
            row = cur.fetchone()
        conn.commit()
    if not row:
        return None
    return {"command_event_id": int(row[0]), "message_id": row[1], "argument": row[2] or "", "attempts": int(row[3])}


def _mark_sent(command_event_id: int, payload: dict) -> None:
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE bp_feishu_command_event
                   SET status='sent', response_payload=%s, replied_at=now(), locked_at=NULL,
                       last_error=NULL, updated_at=now()
                   WHERE command_event_id=%s""",
                (Json(payload), command_event_id),
            )
        conn.commit()


def _mark_failed(command_event_id: int, attempts: int, max_attempts: int, error: str, payload: dict | None) -> None:
    exhausted = attempts >= max_attempts
    delay_index = min(max(attempts - 1, 0), len(_RETRY_DELAYS_SECONDS) - 1)
    next_attempt = datetime.now(timezone.utc) + timedelta(seconds=_RETRY_DELAYS_SECONDS[delay_index])
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE bp_feishu_command_event
                   SET status=%s, response_payload=COALESCE(%s, response_payload),
                       next_attempt_at=%s, locked_at=NULL, last_error=%s, updated_at=now()
                   WHERE command_event_id=%s""",
                ("exhausted" if exhausted else "failed", Json(payload) if payload else None,
                 next_attempt, error[:2000], command_event_id),
            )
        conn.commit()


def dispatch_pending(command_event_id: int | None = None, limit: int = 20) -> dict:
    config = load_command_config()
    if not config.enabled:
        return {"sent": 0, "failed": 0, "disabled": True}
    feishu = notifications.load_feishu_config()
    sent = failed = 0
    for _ in range(max(1, limit)):
        claimed = _claim_one(config.max_attempts, command_event_id)
        if not claimed:
            break
        payload = None
        snapshot = None
        current_id = claimed["command_event_id"]
        try:
            with db.get_conn() as conn:
                payload = query_position(conn, claimed["argument"])
            card = build_position_card(payload)
            snapshot = {"result": payload, "card": card}
            reply_feishu(claimed["message_id"], card, feishu)
            _mark_sent(current_id, snapshot)
            sent += 1
        except Exception as exc:  # noqa: BLE001 - durable retry boundary
            failed += 1
            _mark_failed(current_id, claimed["attempts"], config.max_attempts, str(exc), snapshot)
            logger.warning("飞书命令回复失败 command_event_id=%s: %s", current_id, exc)
        if command_event_id is not None:
            break
    return {"sent": sent, "failed": failed, "disabled": False}
