from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from bp_api import feishu_commands as commands
from bp_api import notifications
from bp_api import feishu_bot


def _enable(monkeypatch):
    monkeypatch.setenv("BP_FEISHU_COMMANDS_ENABLED", "true")
    monkeypatch.setenv("BP_FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("BP_FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("BP_FEISHU_CHAT_ID", "oc_allowed")


def _incoming(**overrides):
    values = {
        "event_id": "evt-1",
        "message_id": "om-1",
        "chat_id": "oc_allowed",
        "chat_type": "group",
        "sender_type": "user",
        "message_type": "text",
        "text": "@_user_1 /PoSiTiOn  2 ",
        "mention_keys": ("@_user_1",),
    }
    values.update(overrides)
    return commands.IncomingCommand(**values)


def test_parser_normalizes_group_mention_and_supports_p2p(monkeypatch):
    _enable(monkeypatch)
    assert commands.accept_incoming(_incoming()) == "2"
    assert commands.accept_incoming(_incoming(chat_type="p2p", chat_id="oc_dm", text=" /position 投资 ", mention_keys=())) == "投资"
    assert commands.accept_incoming(_incoming(text="@_user_1 /other")) is None


def test_parser_ignores_wrong_group_non_text_and_bot(monkeypatch):
    _enable(monkeypatch)
    assert commands.accept_incoming(_incoming(chat_id="oc_other")) is None
    assert commands.accept_incoming(_incoming(mention_keys=())) is None
    assert commands.accept_incoming(_incoming(message_type="image")) is None
    assert commands.accept_incoming(_incoming(sender_type="app")) is None


class _QueryCursor:
    def __init__(self, mode="success"):
        self.mode = mode
        self.rows = []
        self.one = None
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        sql = " ".join(sql.split())
        self.executed.append((sql, params))
        self.rows = []
        self.one = None
        if "WHERE lower(name)=lower" in sql:
            if self.mode == "duplicate":
                self.rows = [(2, "投资"), (7, "投资")]
            elif self.mode == "missing_name":
                self.rows = []
            else:
                self.rows = [(2, "投资")]
        elif "SELECT name, method, status" in sql:
            status = "error" if self.mode == "error" else "done"
            self.one = ("投资", "all_risk_parity", status, "solver failed", date(2026, 8, 25))
        elif "FROM bp_backtest_rebalance" in sql:
            if self.mode != "no_rebalance":
                self.one = (date(2026, 8, 20), {"B@src": 0.4, "A@src": 0.6})
        elif "FROM bp_portfolio_asset" in sql:
            self.rows = [("A", "src", "资产A"), ("B", "src", "资产B")]

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, cursor):
        self.value = cursor

    def cursor(self):
        return self.value


def test_query_reads_current_method_latest_rebalance_and_all_weights():
    cursor = _QueryCursor()
    payload = commands.query_position(_Conn(cursor), "投资")
    assert payload["ok"] is True
    assert payload["method"] == "all_risk_parity"
    assert payload["holding_date"] == "2026-08-20"
    assert [item["name"] for item in payload["holdings"]] == ["资产A", "资产B"]
    assert payload["total_weight"] == pytest.approx(1.0)
    rebalance_query = next(sql for sql, _ in cursor.executed if "FROM bp_backtest_rebalance" in sql)
    assert "method=%s" in rebalance_query and "ORDER BY trade_date DESC LIMIT 1" in rebalance_query


def test_query_uses_public_default_demo(monkeypatch):
    cursor = _QueryCursor()
    get_demo = MagicMock(return_value=2)
    monkeypatch.setattr(commands.repo, "get_demo_id", get_demo)
    payload = commands.query_position(_Conn(cursor), "")
    assert payload["portfolio_id"] == 2
    get_demo.assert_called_once()


def test_query_reports_duplicate_error_and_missing_rebalance():
    duplicate = commands.query_position(_Conn(_QueryCursor("duplicate")), "投资")
    assert duplicate["ok"] is False and "ID 2" in duplicate["message"] and "ID 7" in duplicate["message"]
    missing = commands.query_position(_Conn(_QueryCursor("no_rebalance")), "2")
    assert missing["ok"] is False and "没有调仓" in missing["message"]


def test_position_card_has_dates_all_assets_total_and_dashboard():
    payload = {
        "ok": True,
        "portfolio_id": 2,
        "portfolio_name": "投资*[A]",
        "method_name": "全体风险平价",
        "data_as_of_date": "2026-08-25",
        "holding_date": "2026-08-20",
        "holdings": [{"name": f"资产{i}", "weight": 0.01} for i in range(100)],
        "total_weight": 1.0,
    }
    card = commands.build_position_card(payload, "http://example.test/")
    content = json.dumps(card, ensure_ascii=False)
    assert "2026-08-25" in content and "2026-08-20" in content
    assert "资产99" in content and "100.00%" in content
    assert "http://example.test/dashboard?id=2" in content
    assert "投资\\*\\[A\\]" in card["elements"][0]["text"]["content"]


class _Response:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code

    def json(self):
        return self.body


def _feishu_config():
    return notifications.FeishuConfig(
        app_id="cli_test", app_secret="secret", chat_id="oc_allowed", site_url="",
        max_attempts=5, timeout_seconds=3,
    )


def test_reply_uses_reply_api_and_refreshes_token_once(monkeypatch):
    tokens = iter(("expired", "fresh"))
    get_token = MagicMock(side_effect=lambda *_args, **_kwargs: next(tokens))
    post = MagicMock(side_effect=[
        _Response({"code": 99991663, "msg": "invalid"}, 401),
        _Response({"code": 0, "msg": "success"}),
    ])
    monkeypatch.setattr(commands.notifications, "get_tenant_access_token", get_token)
    monkeypatch.setattr(commands.requests, "post", post)
    commands.reply_feishu("om/a", {"elements": []}, _feishu_config())
    assert post.call_count == 2
    assert post.call_args_list[0].args[0].endswith("/om%2Fa/reply")
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer fresh"
    assert post.call_args.kwargs["json"]["msg_type"] == "interactive"
    assert post.call_args.kwargs["json"]["uuid"]
    assert get_token.call_args_list[1].kwargs == {"force_refresh": True}


def test_command_config_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("BP_FEISHU_COMMANDS_ENABLED", raising=False)
    assert commands.load_command_config().enabled_flag is False


def test_websocket_callback_extracts_only_normalized_fields(monkeypatch):
    ingest = MagicMock()
    monkeypatch.setattr(feishu_bot, "ingest_command", ingest)
    data = SimpleNamespace(
        header=SimpleNamespace(event_id="evt-ws"),
        event=SimpleNamespace(
            sender=SimpleNamespace(sender_type="user"),
            message=SimpleNamespace(
                message_id="om-ws", chat_id="oc_allowed", chat_type="group",
                message_type="text", content=json.dumps({"text": "@_user_1 /position"}),
                mentions=[SimpleNamespace(key="@_user_1")],
            ),
        ),
    )
    feishu_bot._handle_message(data)
    incoming = ingest.call_args.args[0]
    assert incoming.event_id == "evt-ws"
    assert incoming.text == "@_user_1 /position"
    assert incoming.mention_keys == ("@_user_1",)


def test_dispatch_failure_is_persisted_without_raising(monkeypatch):
    _cfg = commands.FeishuCommandConfig(enabled_flag=True, max_attempts=5)
    monkeypatch.setattr(commands, "load_command_config", lambda: _cfg)
    monkeypatch.setattr(commands.notifications, "load_feishu_config", _feishu_config)
    monkeypatch.setattr(commands, "_claim_one", lambda *_args: {
        "command_event_id": 9, "message_id": "om-9", "argument": "2", "attempts": 1,
    })
    monkeypatch.setattr(commands, "query_position", lambda *_args: {"ok": False, "title": "x", "message": "y"})
    monkeypatch.setattr(commands, "reply_feishu", MagicMock(side_effect=ConnectionError("offline")))
    mark_failed = MagicMock()
    monkeypatch.setattr(commands, "_mark_failed", mark_failed)
    fake_conn = MagicMock()
    fake_conn.__enter__.return_value = fake_conn
    fake_conn.__exit__.return_value = False
    monkeypatch.setattr(commands.db, "get_conn", lambda: fake_conn)
    result = commands.dispatch_pending(command_event_id=9, limit=1)
    assert result == {"sent": 0, "failed": 1, "disabled": False}
    mark_failed.assert_called_once()
