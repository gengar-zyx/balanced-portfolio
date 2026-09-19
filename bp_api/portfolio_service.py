"""Portfolio operations shared by REST and MCP; the caller owns the transaction."""
from fastapi import HTTPException

from . import repositories as repo, tasking
from .schemas import CreatePortfolioIn, UpdatePortfolioIn


def validate_payload(payload: CreatePortfolioIn | UpdatePortfolioIn) -> None:
    if payload.method not in repo.BACKTEST_METHODS or payload.ratio not in ('sharpe', 'sortino'):
        raise HTTPException(400, '未知优化方法或比率')
    if payload.benchmark_key not in repo.BENCHMARKS:
        raise HTTPException(400, '未知基准')
    if payload.lookback_days < 2 or not payload.assets:
        raise HTTPException(400, '回溯窗口至少 2 个交易日，且至少选择一个资产')
    if not 0 <= payload.rebalance_band <= 1:
        raise HTTPException(400, '再平衡偏离带须在 [0,1] 之间')
    seen = set()
    for a in payload.assets:
        if a.quadrant not in ('overheat', 'stagflation', 'recovery', 'recession'):
            raise HTTPException(400, '未知象限')
        key = (a.symbol, a.source, a.quadrant)
        if key in seen:
            raise HTTPException(400, f'重复配置: {a.symbol}@{a.source} 在象限 {a.quadrant}')
        seen.add(key)
    if repo.count_unique_assets(payload.assets) * payload.max_weight < .999:
        raise HTTPException(400, '品种数×单资产最大权重必须至少为 100%')


def enqueue_backtest(conn, portfolio_id, owner_user_id, task_type='backtest'):
    active = tasking.find_active_portfolio_task(conn, portfolio_id)
    if active:
        return active
    tid = tasking.create_task(conn, task_type, portfolio_id=portfolio_id,
                              owner_user_id=owner_user_id, progress_total=6, message='回测已排队')
    with conn.cursor() as cur:
        cur.execute("UPDATE bp_portfolio SET status='running', error=NULL WHERE portfolio_id=%s", (portfolio_id,))
    return tid


def mutate(conn, action, user, payload=None, portfolio_id=None, name=None):
    if user.user_id is None:
        raise HTTPException(401, '需要有效用户')
    # Shared with MCP's quota/idempotency lock. Serializes creation and quota checks.
    with conn.cursor() as cur:
        cur.execute('SELECT user_id FROM bp_user WHERE user_id=%s FOR UPDATE', (user.user_id,))
        if portfolio_id is not None:
            cur.execute('SELECT portfolio_id FROM bp_portfolio WHERE portfolio_id=%s FOR UPDATE', (portfolio_id,))
            if not cur.fetchone():
                raise HTTPException(404, '组合不存在或无权限')
    if payload is not None:
        validate_payload(payload)
    if action in ('create', 'copy') and not user.is_admin:
        limit = repo.get_user_portfolio_limit(conn, user.user_id)
        if limit is not None and repo.count_user_portfolios(conn, user.user_id) >= limit:
            raise HTTPException(403, f'每个用户最多创建 {limit} 个投资组合')
    if action == 'create':
        portfolio_id = repo.create_portfolio(conn, payload, user.user_id)
    elif action == 'copy':
        if not repo.can_view_portfolio(conn, portfolio_id, user.user_id, user.is_admin):
            raise HTTPException(404, '组合不存在或无权限')
        portfolio_id = repo.copy_portfolio(conn, portfolio_id, user.user_id, name)
    else:
        if not repo.can_edit_portfolio(conn, portfolio_id, user.user_id, user.is_admin):
            raise HTTPException(404, '组合不存在或无权限')
        status = repo.get_portfolio_status(conn, portfolio_id)
        if status['status'] == 'running':
            active = tasking.find_active_portfolio_task(conn, portfolio_id)
            if action == 'recompute' and active:
                return {'portfolio_id': portfolio_id, 'status': 'running', 'task_id': active, '_reused_task': True}
            raise HTTPException(409, '回测进行中，请稍后再编辑')
        if action == 'update':
            repo.update_portfolio(conn, portfolio_id, payload, user.user_id)
        elif action != 'recompute':
            raise ValueError('Unknown portfolio operation')
    tid = enqueue_backtest(conn, portfolio_id, user.user_id)
    return {'portfolio_id': portfolio_id, 'status': 'running', 'task_id': tid}
