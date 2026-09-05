from __future__ import annotations

from datetime import date

from bp_api import daily_update


class _Cursor:
    def __init__(self, conn: "_Connection") -> None:
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        if "SELECT p.portfolio_id" in sql:
            self._rows = self.conn.portfolios
        else:
            self._rows = []

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, portfolios) -> None:
        self.portfolios = portfolios
        self.executed = []

    def cursor(self):
        return _Cursor(self)


def test_manual_enqueue_retries_failed_portfolio_at_same_trade_date(monkeypatch):
    trade_date = date(2026, 8, 28)
    conn = _Connection([(2, 7, "error", trade_date, trade_date)])

    monkeypatch.setattr(daily_update.tasking, "find_active_portfolio_task", lambda *_: None)
    monkeypatch.setattr(daily_update.tasking, "create_task", lambda *_args, **_kwargs: "task-2")
    monkeypatch.setattr(daily_update.tasking, "enqueue_backtest", lambda *_: "celery-2")
    monkeypatch.setattr(daily_update.tasking, "set_celery_id", lambda *_: None)

    queued = daily_update.enqueue_ready_portfolios(conn, include_failed=True)

    assert queued == [{
        "portfolio_id": 2,
        "task_id": "task-2",
        "target_trade_date": trade_date,
    }]
    assert any("UPDATE bp_portfolio SET status='running'" in sql for sql, _ in conn.executed)


def test_scheduled_enqueue_keeps_same_date_failed_portfolio_idempotent(monkeypatch):
    trade_date = date(2026, 8, 28)
    conn = _Connection([(2, 7, "error", trade_date, trade_date)])
    create_calls = []
    monkeypatch.setattr(
        daily_update.tasking,
        "create_task",
        lambda *_args, **_kwargs: create_calls.append(True),
    )

    queued = daily_update.enqueue_ready_portfolios(conn)

    assert queued == []
    assert create_calls == []


def test_manual_enqueue_still_skips_successful_current_portfolio(monkeypatch):
    trade_date = date(2026, 8, 28)
    conn = _Connection([(2, 7, "done", trade_date, trade_date)])
    create_calls = []
    monkeypatch.setattr(
        daily_update.tasking,
        "create_task",
        lambda *_args, **_kwargs: create_calls.append(True),
    )

    queued = daily_update.enqueue_ready_portfolios(conn, include_failed=True)

    assert queued == []
    assert create_calls == []
