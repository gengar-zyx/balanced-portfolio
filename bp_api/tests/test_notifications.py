from __future__ import annotations

import json
import time
from datetime import date
from unittest.mock import MagicMock

import pytest
import requests

from bp_api import notifications


def _config(**overrides) -> notifications.FeishuConfig:
    values = {
        "app_id": "cli_test",
        "app_secret": "app-secret",
        "chat_id": "oc_test_group",
        "site_url": "https://portfolio.example.com",
        "max_attempts": 5,
        "timeout_seconds": 3.0,
    }
    values.update(overrides)
    return notifications.FeishuConfig(**values)


def _payload() -> dict:
    return {
        "portfolio_id": 7,
        "portfolio_name": "均衡*[组合]",
        "method": "all_risk_parity",
        "method_name": "全体风险平价",
        "trade_date": "2026-08-25",
        "reason": "黄金*偏离目标",
        "rebalance_band": 0.05,
        "max_deviation": 0.0612,
        "assets": [
            {"name": "黄金", "previous": 0.31, "target": 0.25, "delta": -0.06},
            {"name": "股票", "previous": 0.19, "target": 0.25, "delta": 0.06},
        ],
    }


def test_build_card_contains_details_and_link():
    body = notifications.build_feishu_card(_payload(), _config())

    rendered = str(body["elements"])
    summary = body["elements"][0]["text"]["content"]
    assert "31.00% → 25.00%" in rendered
    assert "-6.00pp" in rendered
    assert "均衡\\*\\[组合\\]" in summary
    assert body["elements"][-1]["actions"][0]["url"].endswith("/dashboard?id=7")


def test_build_card_bounds_very_long_asset_lists():
    payload = _payload()
    payload["assets"] = [
        {"name": f"资产-{i}-" + "很长的名称" * 20, "previous": 0.01, "target": 0.02, "delta": 0.01}
        for i in range(500)
    ]
    body = notifications.build_feishu_card(payload, _config())
    encoded = str(body).encode("utf-8")
    assert len(encoded) < 30_000
    assert "其余" in str(body)


class _Response:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code

    def json(self):
        return self.body


def test_get_tenant_token_fetches_and_caches_with_early_expiry(monkeypatch):
    notifications._local_tokens.clear()
    monkeypatch.setattr(notifications.cache, "get_json", lambda _key: None)
    set_json = MagicMock()
    monkeypatch.setattr(notifications.cache, "set_json", set_json)
    post = MagicMock(return_value=_Response({
        "code": 0, "msg": "ok", "tenant_access_token": "tenant-token", "expire": 7200,
    }))
    monkeypatch.setattr(notifications.requests, "post", post)

    assert notifications.get_tenant_access_token(_config()) == "tenant-token"
    assert post.call_args.args[0] == notifications._TOKEN_URL
    assert post.call_args.kwargs["json"] == {"app_id": "cli_test", "app_secret": "app-secret"}
    ttl = set_json.call_args.kwargs["ttl_seconds"]
    assert 6800 <= ttl <= 6900

    post.reset_mock()
    assert notifications.get_tenant_access_token(_config()) == "tenant-token"
    post.assert_not_called()


def test_get_tenant_token_uses_redis_cache(monkeypatch):
    notifications._local_tokens.clear()
    cached = {"token": "redis-token", "expires_at": time.time() + 600}
    monkeypatch.setattr(notifications.cache, "get_json", lambda _key: cached)
    post = MagicMock()
    monkeypatch.setattr(notifications.requests, "post", post)

    assert notifications.get_tenant_access_token(_config()) == "redis-token"
    post.assert_not_called()


def test_get_tenant_token_rejects_business_error_without_exposing_secret(monkeypatch):
    notifications._local_tokens.clear()
    monkeypatch.setattr(notifications.cache, "get_json", lambda _key: None)
    monkeypatch.setattr(
        notifications.requests,
        "post",
        MagicMock(return_value=_Response({"code": 10003, "msg": "invalid app"})),
    )

    with pytest.raises(notifications.FeishuApiError, match="10003") as exc_info:
        notifications.get_tenant_access_token(_config())
    assert "app-secret" not in str(exc_info.value)


def test_send_feishu_posts_interactive_message_to_fixed_chat(monkeypatch):
    monkeypatch.setattr(notifications, "get_tenant_access_token", lambda _cfg, **_kwargs: "tenant-token")
    post = MagicMock(return_value=_Response({"code": 0, "msg": "success", "data": {}}))
    monkeypatch.setattr(notifications.requests, "post", post)

    notifications.send_feishu(_payload(), _config())

    assert post.call_args.args[0] == notifications._MESSAGE_URL
    assert post.call_args.kwargs["params"] == {"receive_id_type": "chat_id"}
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer tenant-token"
    assert post.call_args.kwargs["timeout"] == 3.0
    request_body = post.call_args.kwargs["json"]
    assert request_body["receive_id"] == "oc_test_group"
    assert request_body["msg_type"] == "interactive"
    assert json.loads(request_body["content"])["header"]["template"] == "orange"


def test_send_feishu_rejects_non_auth_business_error(monkeypatch):
    monkeypatch.setattr(notifications, "get_tenant_access_token", lambda _cfg, **_kwargs: "tenant-token")
    monkeypatch.setattr(
        notifications.requests,
        "post",
        MagicMock(return_value=_Response({"code": 230001, "msg": "no permission"})),
    )
    with pytest.raises(RuntimeError, match="230001"):
        notifications.send_feishu(_payload(), _config())


def test_send_feishu_refreshes_expired_token_once(monkeypatch):
    tokens = iter(("expired-token", "fresh-token"))
    get_token = MagicMock(side_effect=lambda _cfg, **_kwargs: next(tokens))
    monkeypatch.setattr(notifications, "get_tenant_access_token", get_token)
    post = MagicMock(side_effect=[
        _Response({"code": 99991663, "msg": "invalid token"}, status_code=401),
        _Response({"code": 0, "msg": "success", "data": {}}),
    ])
    monkeypatch.setattr(notifications.requests, "post", post)

    notifications.send_feishu(_payload(), _config())

    assert post.call_count == 2
    assert get_token.call_count == 2
    assert get_token.call_args_list[1].kwargs == {"force_refresh": True}
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer fresh-token"


class _Cursor:
    def __init__(self, task_type="daily_update", inserted_id=11):
        self.task_type = task_type
        self.inserted_id = inserted_id
        self.result = None
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        if "SELECT task_type FROM bp_task" in sql:
            self.result = (self.task_type,)
        elif "INSERT INTO bp_notification_outbox" in sql:
            self.result = (self.inserted_id,) if self.inserted_id is not None else None

    def fetchone(self):
        return self.result


class _Connection:
    def __init__(self, cursor):
        self.cursor_value = cursor

    def cursor(self):
        return self.cursor_value


def _enqueue(monkeypatch, cursor, **overrides):
    monkeypatch.setenv("BP_FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("BP_FEISHU_APP_SECRET", "app-secret")
    monkeypatch.setenv("BP_FEISHU_CHAT_ID", "oc_test_group")
    values = {
        "task_id": "task-1",
        "portfolio_id": 7,
        "portfolio_name": "组合",
        "method": "all_risk_parity",
        "method_name": "全体风险平价",
        "trade_date": date(2026, 8, 25),
        "reason": "偏离带触发",
        "rebalance_band": 0.05,
        "max_deviation": 0.06,
        "target_weights": {"A": 0.4, "B": 0.6},
        "prev_weights": {"A": 0.5, "B": 0.5},
        "delta": {"A": -0.1, "B": 0.1},
        "asset_names": {"A": "资产A", "B": "资产B"},
    }
    values.update(overrides)
    return notifications.enqueue_rebalance_event(_Connection(cursor), **values)


def test_enqueue_daily_update_creates_idempotent_snapshot(monkeypatch):
    cursor = _Cursor()
    assert _enqueue(monkeypatch, cursor) == 11
    insert = next(call for call in cursor.executed if "INSERT INTO bp_notification_outbox" in call[0])
    assert "ON CONFLICT" in insert[0]
    snapshot = insert[1][-1].obj
    assert snapshot["trade_date"] == "2026-08-25"
    assert {row["name"] for row in snapshot["assets"]} == {"资产A", "资产B"}


def test_enqueue_ignores_manual_backtest_and_inception(monkeypatch):
    manual = _Cursor(task_type="backtest")
    assert _enqueue(monkeypatch, manual) is None
    assert not any("INSERT INTO bp_notification_outbox" in sql for sql, _ in manual.executed)

    inception = _Cursor()
    assert _enqueue(monkeypatch, inception, reason="建仓") is None
    assert not any("INSERT INTO bp_notification_outbox" in sql for sql, _ in inception.executed)


def test_enqueue_is_disabled_with_incomplete_app_config(monkeypatch):
    monkeypatch.setenv("BP_FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("BP_FEISHU_APP_SECRET", "app-secret")
    monkeypatch.delenv("BP_FEISHU_CHAT_ID", raising=False)
    cursor = _Cursor()
    result = notifications.enqueue_rebalance_event(
        _Connection(cursor),
        task_id="task-1", portfolio_id=7, portfolio_name="组合",
        method="all_risk_parity", method_name="全体风险平价",
        trade_date=date(2026, 8, 25), reason="偏离带触发",
        rebalance_band=0.05, max_deviation=0.06,
        target_weights={}, prev_weights={}, delta={}, asset_names={},
    )
    assert result is None
    assert cursor.executed == []


def test_dispatch_persists_network_failure_without_raising(monkeypatch):
    claimed = {"notification_id": 11, "payload": _payload(), "attempts": 1}
    monkeypatch.setattr(notifications, "load_feishu_config", lambda: _config())
    monkeypatch.setattr(notifications, "_claim_one", lambda *_args, **_kwargs: claimed)
    monkeypatch.setattr(
        notifications,
        "send_feishu",
        MagicMock(side_effect=requests.ConnectionError("offline")),
    )
    mark_failed = MagicMock()
    monkeypatch.setattr(notifications, "_mark_failed", mark_failed)

    result = notifications.dispatch_pending(notification_id=11, limit=1)

    assert result == {"sent": 0, "failed": 1, "disabled": False}
    mark_failed.assert_called_once()
