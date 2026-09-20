"""Agent-facing business tools, independent of MCP transport and REST responses."""
from __future__ import annotations

import hashlib
import inspect
import json
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal, get_type_hints
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator
from psycopg.types.json import Jsonb

from . import db, repositories as repo, repositories_otc as rotc, portfolio_service, tasking
from .agent_auth import AgentIdentity
from .schemas import CreatePortfolioIn as RestCreatePortfolioIn, UpdatePortfolioIn as RestUpdatePortfolioIn
from .schemas_otc import OtcPriceIn as RestOtcPriceIn, ObservationDatesIn


class CreatePortfolioIn(RestCreatePortfolioIn):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class UpdatePortfolioIn(RestUpdatePortfolioIn):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class OtcPriceIn(RestOtcPriceIn):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    t_step_per_year: int = Field(default=252, ge=1, le=366)
    ko_freq_months: int = Field(default=1, ge=1, le=360)
    lock_term_months: int = Field(default=0, ge=0, le=360)
    spot: float | None = Field(default=None, gt=0)
    day_count: Literal['ACT365', 'ACT360', 'BUS252'] = 'ACT365'

    @model_validator(mode='after')
    def bounded_horizon(self):
        if (self.maturity_date - self.start_date).days > 366 * 30:
            raise ValueError('OTC 期限最多 30 年')
        return self

identity: ContextVar[AgentIdentity] = ContextVar('bp_agent_identity')
Limit = Annotated[int, Field(ge=1, le=500)]
Offset = Annotated[int, Field(ge=0)]
ObjectId = Annotated[int, Field(gt=0)]
RequestId = Annotated[str, Field(min_length=1, max_length=128, pattern=r'^\S+$')]
Variety = Literal['IF', 'IH', 'IC', 'IM']


class ToolError(Exception):
    def __init__(self, code, message, retryable=False):
        self.code, self.message, self.retryable = code, message, retryable
        super().__init__(message)


class ErrorInfo(BaseModel):
    code: str
    message: str
    retryable: bool = False


class ToolOutput(BaseModel):
    request_id: str
    data: dict[str, Any] | None = None
    error: ErrorInfo | None = None


@dataclass
class ToolDefinition:
    name: str
    description: str
    model: type[BaseModel]
    fn: Any
    scopes: frozenset[str]


TOOLS: dict[str, ToolDefinition] = {}


def tool(*scopes):
    def register(fn):
        hints = get_type_hints(fn, include_extras=True)
        fields = {n: (hints[n], ... if p.default is inspect.Parameter.empty else p.default)
                  for n, p in inspect.signature(fn).parameters.items()}
        model = create_model(fn.__name__ + '_input', __config__=ConfigDict(extra='forbid'), **fields)
        TOOLS[fn.__name__] = ToolDefinition(fn.__name__, fn.__doc__ or '', model, fn, frozenset(scopes))
        return fn
    return register


def page(items, limit=100, offset=0):
    total = len(items)
    return {'items': items[offset:offset + limit], 'total': total,
            'next_offset': offset + limit if offset + limit < total else None}


def dated_page(items, start_date, end_date, limit, offset):
    check_dates(start_date, end_date)
    items = [r for r in items if (not start_date or str(r['trade_date']) >= str(start_date))
             and (not end_date or str(r['trade_date']) <= str(end_date))]
    return page(items, limit, offset)


def check_dates(start_date, end_date):
    if start_date and end_date and start_date > end_date:
        raise ToolError('INVALID_ARGUMENT', '起始日期不能晚于结束日期')


def visible_portfolio(conn, pid, edit=False):
    user = identity.get()
    fn = repo.can_edit_portfolio if edit else repo.can_view_portfolio
    try:
        allowed = fn(conn, pid, user.user_id, False)
    except KeyError:
        allowed = False
    if not allowed:
        raise ToolError('NOT_FOUND', '组合不存在或无权限')


def visible_task(conn, tid):
    uid = identity.get().user_id
    try:
        task = tasking.get_task(conn, str(tid))
    except KeyError:
        raise ToolError('NOT_FOUND', '任务不存在或无权限')
    if task['task_type'] not in ('backtest', 'daily_update', 'otc_price'):
        raise ToolError('NOT_FOUND', '任务不存在或无权限')
    allowed = task['owner_user_id'] == uid
    if task['owner_user_id'] is None and task.get('portfolio_id'):
        with conn.cursor() as cur:
            cur.execute('SELECT owner_user_id FROM bp_portfolio WHERE portfolio_id=%s', (task['portfolio_id'],))
            row = cur.fetchone()
            allowed = bool(row and row[0] == uid)
    if not allowed:
        raise ToolError('NOT_FOUND', '任务不存在或无权限')
    return task


def submit(tool_name, request_id, params, operation):
    """Idempotency, quota, business mutation and durable dispatch share one commit."""
    agent = identity.get()
    digest = hashlib.sha256(json.dumps(params, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM bp_user WHERE user_id=%s AND status='active' FOR UPDATE", (agent.user_id,))
            if not cur.fetchone():
                raise ToolError('UNAUTHORIZED', '用户不可用')
            cur.execute('DELETE FROM bp_agent_request WHERE owner_user_id=%s AND expires_at<=now()', (agent.user_id,))
            cur.execute('SELECT params_hash,response FROM bp_agent_request WHERE owner_user_id=%s AND tool=%s AND request_id=%s',
                        (agent.user_id, tool_name, request_id))
            old = cur.fetchone()
            if old:
                if old[0] != digest:
                    raise ToolError('CONFLICT', '该 request_id 已用于不同参数')
                return old[1]
            # Recompute may reuse an already active task without consuming quota.
            reuse = None
            if tool_name == 'run_backtest':
                visible_portfolio(conn, params['portfolio_id'], edit=True)
                reuse = tasking.find_active_portfolio_task(conn, params['portfolio_id'])
            cur.execute("SELECT count(*) FROM bp_task WHERE owner_user_id=%s AND initiated_via='mcp' AND status IN ('queued','running')", (agent.user_id,))
            if cur.fetchone()[0] >= 2 and not reuse:
                raise ToolError('COMPUTE_LIMIT', '最多同时运行 2 个计算任务，请稍后重试', True)
        result, dispatch = operation(conn)
        tid = str(result['task_id'])
        with conn.cursor() as cur:
            if dispatch:
                # Do not take ownership of a task that already existed before this request.
                cur.execute('''UPDATE bp_task SET initiated_via='mcp', dispatch_payload=%s
                    WHERE task_id=%s AND initiated_via IS NULL AND celery_id IS NULL AND status='queued' ''',
                    (Jsonb(dispatch), tid))
            current = tasking.get_task(conn, tid)
            result = {'task_id': tid, 'status': current['status'], 'poll_after_seconds': 2,
                      **({'portfolio_id': result['portfolio_id'], 'portfolio_status': result.get('status')} if 'portfolio_id' in result else {})}
            cur.execute('''INSERT INTO bp_agent_request (owner_user_id,tool,request_id,params_hash,response)
                VALUES (%s,%s,%s,%s,%s)''', (agent.user_id, tool_name, request_id, digest, Jsonb(result)))
        conn.commit()
    return result


def portfolio_submit(tool_name, action, request_id, payload=None, portfolio_id=None, name=None):
    params = {'payload': payload.model_dump(mode='json') if payload else None,
              'portfolio_id': portfolio_id, 'name': name}
    def operation(conn):
        existing = tasking.find_active_portfolio_task(conn, portfolio_id) if portfolio_id else None
        result = portfolio_service.mutate(conn, action, identity.get().user, payload, portfolio_id, name)
        dispatch = None if str(result['task_id']) == str(existing) else {
            'task_name': 'bp_api.backtest', 'kwargs': {'task_id': str(result['task_id']), 'portfolio_id': result['portfolio_id']}}
        return result, dispatch
    return submit(tool_name, request_id, params, operation)


@tool('read')
def get_capabilities() -> dict:
    """Discover valid methods, benchmarks, units and limits before constructing a portfolio or OTC pricing request."""
    from bp_ingest.crypto_corr import ASSET_PAIRS, METHODS, WINDOWS, LAG_PERIODS
    return {'methods': repo.BACKTEST_METHODS, 'benchmarks': repo.BENCHMARKS,
            'quadrants': ['overheat', 'stagflation', 'recovery', 'recession'], 'ratios': ['sharpe', 'sortino'],
            'otc_engines': {'snowball': ['mc'], 'phoenix': ['mc'], 'airbag': ['mc', 'analytic', 'quad'], 'barrier': ['mc', 'analytic', 'quad']},
            'crypto': {'assets': ASSET_PAIRS, 'methods': METHODS, 'windows': WINDOWS, 'lags': LAG_PERIODS},
            'units': {'rates': 'decimal: 0.02 = 2%', 'barrier_pct': 'percent: 103 = 103%',
                      'dates': 'YYYY-MM-DD', 'asset_key': 'symbol@source'},
            'limits': {'max_page_size': 500, 'active_compute_per_user': 2, 'idempotency_hours': 24},
            'workflow': 'list_assets → create_portfolio → get_task → get_backtest_result; get_target_allocation → compare with external actual holdings; price_otc → get_task → get_task_result'}


@tool('read')
def list_assets(keyword: str = '', category: str | None = None, limit: Limit = 100, offset: Offset = 0) -> dict:
    """Discover selectable assets and actual cleaned-price date ranges. Use symbol and source together."""
    with db.get_conn() as conn:
        items = repo.list_assets(conn)
        with conn.cursor() as cur:
            cur.execute('SELECT symbol,source,min(trade_date),max(trade_date) FROM bp_quote_clean GROUP BY symbol,source')
            ranges = {(r[0], r[1]): (r[2], r[3]) for r in cur.fetchall()}
    for item in items:
        item['asset_key'] = repo.asset_key(item['symbol'], item['source'])
        item['data_start'], item['data_end'] = ranges.get((item['symbol'], item['source']), (None, None))
    return page([i for i in items if (not category or i.get('category') == category)
                 and keyword.casefold() in f"{i.get('name', '')} {i['asset_key']}".casefold()], limit, offset)


@tool('read')
def list_portfolios(limit: Limit = 100, offset: Offset = 0) -> dict:
    """List your portfolios and public examples. Administrator access is not inherited."""
    with db.get_conn() as conn:
        items = repo.list_portfolios(conn, identity.get().user_id, False)
        return page([portfolio_view(item) for item in items], limit, offset)


def portfolio_view(item):
    # Stored worker exception text can include database details. Keep it out of
    # the agent API just as we do for live exceptions and task errors.
    return {**item, 'error': '回测失败，请检查输入并重算' if item.get('error') else None}


@tool('read')
def get_portfolio(portfolio_id: ObjectId) -> dict:
    """Read the full current portfolio configuration before updating it."""
    with db.get_conn() as conn:
        visible_portfolio(conn, portfolio_id)
        return portfolio_view(repo.get_portfolio_dict(conn, portfolio_id))


@tool('portfolio:write', 'compute')
def create_portfolio(request_id: RequestId, payload: CreatePortfolioIn) -> dict:
    """Create your portfolio and automatically queue backtesting. Rates are decimals; Sharpe requires risk_free_rate. Retry with the SAME request_id and parameters."""
    return portfolio_submit('create_portfolio', 'create', request_id, payload)


@tool('portfolio:write', 'compute')
def update_portfolio(request_id: RequestId, portfolio_id: ObjectId, payload: UpdatePortfolioIn) -> dict:
    """Replace your portfolio's full configuration and queue backtesting. Read get_portfolio first; running portfolios cannot be edited. Retry with the same request_id."""
    return portfolio_submit('update_portfolio', 'update', request_id, payload, portfolio_id)


@tool('portfolio:write', 'compute')
def copy_portfolio(request_id: RequestId, portfolio_id: ObjectId, name: str | None = None) -> dict:
    """Copy a visible portfolio into your account and automatically backtest. Retry with the same request_id."""
    return portfolio_submit('copy_portfolio', 'copy', request_id, portfolio_id=portfolio_id, name=name)


@tool('compute')
def run_backtest(request_id: RequestId, portfolio_id: ObjectId) -> dict:
    """Recompute your portfolio, reusing an active task when present. Retry with the same request_id."""
    return portfolio_submit('run_backtest', 'recompute', request_id, portfolio_id=portfolio_id)


def completed_backtest_result(portfolio_id, method=None, benchmark=None):
    """Read authorization, status, result and version from one database snapshot."""
    if method and method not in repo.BACKTEST_METHODS or benchmark and benchmark not in repo.BENCHMARKS:
        raise ToolError('INVALID_ARGUMENT', '未知方法或基准')
    with db.get_conn() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        visible_portfolio(conn, portfolio_id)
        st = repo.get_portfolio_status(conn, portfolio_id)
        if st['status'] != 'done':
            raise ToolError('COMPUTE_FAILED' if st['status'] == 'error' else 'NOT_READY',
                            '回测失败，请检查输入并重算' if st['status'] == 'error' else '回测尚未完成，请查询任务进度', st['status'] != 'error')
        return repo.get_result(conn, portfolio_id, method, benchmark)


@tool('read')
def get_backtest_result(portfolio_id: ObjectId, method: str | None = None, benchmark: str | None = None,
                        section: Literal['summary', 'nav', 'rebalances', 'corr', 'attribution'] = 'summary',
                        start_date: date | None = None, end_date: date | None = None,
                        limit: Limit = 100, offset: Offset = 0) -> dict:
    """Read a completed backtest. Holdings are strategy targets from the last simulated rebalance, not actual account holdings. Request each large result section separately."""
    check_dates(start_date, end_date)
    result = completed_backtest_result(portfolio_id, method, benchmark)
    meta = {'portfolio': result['portfolio'], 'method': result.get('method'), 'benchmark': result.get('benchmark')}
    if section == 'summary':
        return {k: v for k, v in result.items() if k not in ('nav', 'rebalances', 'corr', 'attribution')}
    value = result.get(section)
    if section in ('nav', 'rebalances'):
        value = dated_page(value or [], start_date, end_date, limit, offset)
    elif section == 'attribution' and isinstance(value, dict):
        value = {k: (dated_page(v, start_date, end_date, limit, offset) if v and all(isinstance(r, dict) and 'trade_date' in r for r in v)
                     else page(v, limit, offset)) if isinstance(v, list) else v for k, v in value.items()}
    return {**meta, section: value}


def allocation_decimal(value):
    """Preserve stored decimal values without silently turning missing data into zero."""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ToolError('NOT_READY', '目标配置包含缺失或无效权重，请重新计算')
    if not number.is_finite() or not 0 <= number <= 1:
        raise ToolError('NOT_READY', '目标配置包含缺失或无效权重，请重新计算')
    return format(number.normalize(), 'f')


@tool('read')
def get_target_allocation(portfolio_id: ObjectId,
                          basis: Literal['last_rebalance', 'latest_optimal'] = 'last_rebalance',
                          method: str | None = None) -> dict:
    """Read strategy target weights for comparison with external actual holdings. last_rebalance uses the last simulated rebalance target; latest_optimal uses the final optimization date. Neither is an actual account position or a drifted current weight. Missing requested data never falls back to another basis/method. Weights are decimal strings; use allocation_date/data_as_of_date to check freshness before advice."""
    result = completed_backtest_result(portfolio_id, method)
    portfolio = result['portfolio']
    selected = result.get('method')
    # The dashboard repository falls back to available methods. An advice input
    # must preserve the selected strategy rather than silently substituting one.
    expected = method if method is not None else portfolio.get('method')
    if not selected or selected != expected:
        raise ToolError('NOT_READY', '所选优化方法尚无结果，请重新计算')
    names = {repo.asset_key(a['symbol'], a['source']): a.get('display_name') or a['symbol']
             for a in portfolio.get('assets', [])}
    if basis == 'last_rebalance':
        rebalances = result.get('rebalances') or []
        latest = max(rebalances, key=lambda r: str(r['trade_date'])) if rebalances else {}
        allocation_date = latest.get('trade_date')
        weights = [{'asset_key': key, 'name': names.get(key, key), 'weight': allocation_decimal(value)}
                   for key, value in (latest.get('target_weights') or {}).items()]
    elif basis == 'latest_optimal':
        optimal = result.get('optimal_holdings') or {}
        allocation_date = optimal.get('as_of_date')
        weights = [{'asset_key': row['key'], 'name': row.get('name') or names.get(row['key'], row['key']),
                    'weight': allocation_decimal(row.get('weight'))}
                   for row in optimal.get('holdings') or []]
    else:
        raise ToolError('INVALID_ARGUMENT', '未知目标权重口径')
    if not allocation_date or not weights:
        raise ToolError('NOT_READY', '所选目标权重口径尚无结果，请重新计算')
    if len({row['asset_key'] for row in weights}) != len(weights):
        raise ToolError('NOT_READY', '目标配置包含重复资产，请重新计算')
    total = sum(Decimal(row['weight']) for row in weights)
    # Backtests persist rounded weights (six decimal places), not normalized
    # floating point values. Preserve that precision and reject incomplete data.
    if abs(total - 1) > Decimal('0.000001') * len(weights):
        raise ToolError('NOT_READY', '目标配置权重不完整，请重新计算')
    if not portfolio.get('data_as_of_date') or not portfolio.get('result_version'):
        raise ToolError('NOT_READY', '目标配置缺少数据日期或结果版本，请重新计算')
    allocation = {
        'portfolio_id': portfolio_id, 'name': portfolio['name'], 'method': selected, 'basis': basis,
        'allocation_kind': 'strategy_target', 'weight_unit': 'decimal',
        'allocation_date': str(allocation_date), 'data_as_of_date': str(portfolio['data_as_of_date']),
        'result_version': portfolio['result_version'],
        'weights': sorted(weights, key=lambda row: row['asset_key']),
    }
    if portfolio.get('rebalance_band') is not None:
        allocation['rebalance_band'] = allocation_decimal(portfolio['rebalance_band'])
    allocation['allocation_id'] = hashlib.sha256(
        json.dumps(allocation, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    ).hexdigest()
    return allocation


@tool('read')
def get_cffex_snapshot() -> dict:
    """Read the last confirmed same-trading-day CFFEX close, never an intraday quote."""
    from .cffex import _handle_spot
    return _handle_spot()


@tool('read')
def get_cffex_history(variety: Variety | None = None, start_date: date | None = None,
                       end_date: date | None = None, limit: Limit = 100, offset: Offset = 0) -> dict:
    """Read annualized premium and index closes on a shared date axis, paginated as dated rows."""
    from .cffex import _handle_history
    check_dates(start_date, end_date)
    data = _handle_history(variety, days=0, start_date=str(start_date) if start_date else None, end_date=str(end_date) if end_date else None)
    rows = [{'trade_date': d, 'series': {k: {'ann_premium_rate': v['ann_premium_rates'][i], 'index_price': v['index_prices'][i]}
             for k, v in data['series'].items()}} for i, d in enumerate(data['dates'])]
    return {'calendar': 'CFFEX', **dated_page(rows, start_date, end_date, limit, offset)}


@tool('read')
def get_cffex_statistics(variety: Variety | None = None, period: Literal['3M', '6M', '1Y', '3Y', '5Y'] = '3Y') -> dict:
    """Read historical CFFEX premium percentiles for the selected period."""
    from .cffex import _handle_statistics
    return _handle_statistics(variety, period)


@tool('read')
def get_crypto_correlation(section: Literal['snapshot', 'rolling', 'lagged'] = 'snapshot',
                           asset: str | None = None, method: str = 'pearson', window: str = '3M', lag: str = '3M',
                           start_date: date | None = None, end_date: date | None = None,
                           limit: Limit = 100, offset: Offset = 0) -> dict:
    """Read crypto snapshots, rolling correlations or BTC/DXY shifted series. Discover asset/window/lag keys via get_capabilities. Keeps the source NYSE date axis."""
    from .crypto import get_correlation_payload
    from bp_ingest.crypto_corr import ASSET_PAIRS, METHODS, WINDOWS, LAG_PERIODS
    check_dates(start_date, end_date)
    if asset and asset not in ASSET_PAIRS or method not in METHODS or window not in WINDOWS or lag not in LAG_PERIODS:
        raise ToolError('INVALID_ARGUMENT', '未知资产、算法、窗口或滞后周期，请先查询 get_capabilities')
    data = get_correlation_payload()
    meta = {'is_ready': data['is_ready'], 'meta': data['meta']}
    if not data['is_ready']:
        return meta
    if section == 'snapshot':
        series = data['rolling'].get(window, {}).get(method, {})
        as_of = data['meta'].get('effective_td')
        idx = data['dates'].index(as_of) if as_of in data['dates'] else None
        correlations = {k: v['correlation'][idx] if idx is not None and idx < len(v['correlation']) else None
                        for k, v in series.items() if not asset or k == asset}
        return {**meta, 'snapshot': data['snapshot'], 'correlations': correlations, 'method': method, 'window': window}
    if section == 'lagged':
        series = data['lagged_shifted'][lag]
        rows = [{'trade_date': d, 'btc': series['btc'][i], 'dxy': series['dxy'][i]} for i, d in enumerate(series['dates'])]
    else:
        series = data['rolling'].get(window, {}).get(method, {})
        selected = [asset] if asset else list(series)
        rows = [{'trade_date': d, 'btc_price': data['btc_prices'][i],
                 'correlations': {k: series[k]['correlation'][i] if i < len(series[k]['correlation']) else None for k in selected if k in series}}
                for i, d in enumerate(data['dates'])]
    return {**meta, **dated_page(rows, start_date, end_date, limit, offset)}


@tool('read')
def list_otc_underlyings(limit: Limit = 100, offset: Offset = 0) -> dict:
    """List available OTC underlying indices."""
    with db.get_conn() as conn:
        return page(rotc.list_underlyings(conn), limit, offset)


@tool('read')
def get_otc_market_inputs(symbol: str, source: str | None = None, on_date: date | None = None,
                          window: Annotated[int, Field(ge=5, le=1000)] = 90) -> dict:
    """Read a historical close and latest realized volatility (decimal). Volatility is latest available, not an as-of estimate for on_date."""
    with db.get_conn() as conn:
        src = source or rotc.resolve_source(conn, symbol, None)
        return {'symbol': symbol, 'source': src, 'spot': rotc.spot_on_date(conn, symbol, source=src, on_date=on_date),
                'volatility': rotc.realized_vol_estimate(conn, symbol, src, window) if src else None,
                'window': window, 'volatility_basis': 'latest_available', 'volatility_unit': 'decimal'}


@tool('read')
def get_otc_observation_dates(payload: ObservationDatesIn, limit: Limit = 100, offset: Offset = 0) -> dict:
    """Generate following-adjusted OTC observation dates on the existing CN trading calendar."""
    from .otc_market_service import observation_dates
    return {'calendar': 'CN', **page(observation_dates(payload)['dates'], limit, offset)}


@tool('read')
def list_otc_deals(limit: Limit = 100, offset: Offset = 0) -> dict:
    """List your stored OTC deals and public examples; no contract mutations are exposed."""
    with db.get_conn() as conn:
        items = rotc.list_otc_deals(conn, identity.get().user_id, False)
    # List views should not return every historical chart for every deal.
    return page([{k: v for k, v in item.items() if k != 'last_result'} for item in items], limit, offset)


@tool('read')
def get_otc_deal(deal_id: ObjectId, section: Literal['summary', 'chart', 'events', 'observation_dates'] = 'summary',
                 start_date: date | None = None, end_date: date | None = None,
                 limit: Limit = 100, offset: Offset = 0) -> dict:
    """Read a visible stored OTC contract and its existing valuation; chart and event sections are paginated."""
    with db.get_conn() as conn:
        if not rotc.can_view_otc_deal(conn, deal_id, identity.get().user_id, False):
            raise ToolError('NOT_FOUND', '合约不存在或无权限')
        result = rotc.get_otc_deal(conn, deal_id)
    if result is None:
        raise ToolError('NOT_FOUND', '合约不存在或无权限')
    if result.get('last_result'):
        result['last_result'] = otc_result_view(result['last_result'], section, start_date, end_date, limit, offset)
    return result


def summarize_otc(result):
    # Keep scalar/object summary fields; path vectors are retrieved separately.
    return {k: v for k, v in result.items() if not isinstance(v, list) and k not in ('paths', 'chart', 'path_chart')}


def otc_result_view(result, section, start_date=None, end_date=None, limit=100, offset=0):
    check_dates(start_date, end_date)
    chart = result.get('chart') or {}
    if section == 'summary':
        return summarize_otc(result)
    if section == 'chart':
        dates = chart.get('dates', [])
        arrays = {k: v for k, v in chart.items() if isinstance(v, list) and len(v) == len(dates)
                  and k in ('underlying', 'baseline_line', 'pnl', 'forward_mean', 'mean_path')}
        rows = [{'trade_date': d, **{k: v[i] for k, v in arrays.items()}} for i, d in enumerate(dates)]
        return {'meta': {k: v for k, v in chart.items() if not isinstance(v, list)},
                **dated_page(rows, start_date, end_date, limit, offset)}
    if section == 'events':
        rows = chart.get('events', [])
        return page([r for r in rows if (not start_date or str(r.get('date', '')) >= str(start_date))
                     and (not end_date or str(r.get('date', '')) <= str(end_date))], limit, offset)
    if section == 'observation_dates':
        rows = [{'trade_date': d} for d in chart.get('ko_observation_dates', [])]
        return dated_page(rows, start_date, end_date, limit, offset)
    raise ToolError('INVALID_ARGUMENT', '未知结果分区')


@tool('compute')
def price_otc(request_id: RequestId, payload: OtcPriceIn) -> dict:
    """Submit temporary OTC pricing without saving a deal. Rates/vol are decimals; *_pct barriers are percentages (103 = 103%). Poll get_task, then get_task_result. Retry with the same request_id."""
    supported = ['mc'] if payload.product_type in ('snowball', 'phoenix') else ['mc', 'analytic', 'quad']
    if payload.engine not in supported:
        raise ToolError('INVALID_ARGUMENT', '该产品不支持所选引擎，请查询 get_capabilities')
    spec = payload.to_terms()
    def operation(conn):
        tid = tasking.create_task(conn, 'otc_price', owner_user_id=identity.get().user_id, progress_total=7, message='定价已排队')
        return {'task_id': tid}, {'task_name': 'bp_api.price_otc', 'kwargs': {'task_id': tid, 'spec': spec, 'deal_id': None}}
    return submit('price_otc', request_id, spec, operation)


@tool('read')
def get_task(task_id: UUID) -> dict:
    """Read your task's queued/running/success/failed state and progress. Poll at least two seconds apart."""
    with db.get_conn() as conn:
        task = visible_task(conn, task_id)
    return {k: v for k, v in task.items() if k not in ('result', 'error', 'celery_id', 'owner_user_id')} | {
        'error': '计算失败，请检查输入并重新提交' if task['status'] == 'failed' else None, 'poll_after_seconds': 2}


@tool('read')
def get_task_result(task_id: UUID, section: str = 'summary', start_date: date | None = None,
                    end_date: date | None = None, limit: Limit = 100, offset: Offset = 0) -> dict:
    """Read a completed task. Backtests return portfolio_id for get_backtest_result. OTC defaults to summary; available_sections lists paginated arrays."""
    with db.get_conn() as conn:
        task = visible_task(conn, task_id)
    if task['status'] != 'success':
        raise ToolError('COMPUTE_FAILED' if task['status'] in ('failed', 'cancelled') else 'NOT_READY',
                        '计算未成功完成，请查询任务状态', task['status'] in ('queued', 'running'))
    result = task['result']
    sections = [k for k, v in result.items() if isinstance(v, list)]
    if 'chart' in result:
        sections += ['chart', 'events', 'observation_dates']
    if section == 'summary':
        return {'result': summarize_otc(result), 'available_sections': sections}
    if section not in sections:
        raise ToolError('INVALID_ARGUMENT', '未知结果分区，请先读取 summary')
    if section in ('chart', 'events', 'observation_dates'):
        return {section: otc_result_view(result, section, start_date, end_date, limit, offset)}
    return {section: page(result[section], limit, offset)}
