"""Strategy target contracts for agents comparing an external account snapshot."""
from contextlib import nullcontext
from datetime import date
from unittest.mock import Mock

import pytest

from bp_api import agent_tools as t, mcp_server
from bp_api.agent_auth import AgentIdentity


@pytest.fixture
def allocation_repo(monkeypatch):
    token = t.identity.set(AgentIdentity(11, 'test-token', frozenset({'read'})))
    connection = Mock()
    monkeypatch.setattr(t.db, 'get_conn', lambda: nullcontext(connection))
    allowed = Mock(return_value=True)
    status = Mock(return_value={'status': 'done'})
    result = {
        'portfolio': {
            'portfolio_id': 7, 'name': '策略组合', 'method': 'all_risk_parity',
            'data_as_of_date': date(2026, 9, 18), 'result_version': 9, 'rebalance_band': .05,
            'assets': [
                {'symbol': 'A', 'source': 'fund', 'display_name': '基金 A'},
                {'symbol': 'B', 'source': 'fund', 'display_name': '基金 B'},
            ],
        },
        'method': 'all_risk_parity',
        'rebalances': [
            {'trade_date': date(2026, 9, 10), 'target_weights': {'B@fund': .4, 'A@fund': .6}},
            {'trade_date': date(2026, 9, 1), 'target_weights': {'A@fund': .5, 'B@fund': .5}},
        ],
        # Deliberately differs: this dashboard projection is not the source of
        # last_rebalance, and neither set of targets is an actual holding.
        'holdings': [{'key': 'A@fund', 'weight': .9}, {'key': 'B@fund', 'weight': .1}],
        'optimal_holdings': {
            'as_of_date': date(2026, 9, 18),
            'holdings': [{'key': 'A@fund', 'name': '基金 A', 'weight': .7},
                         {'key': 'B@fund', 'name': '基金 B', 'weight': .3}],
        },
    }
    read = Mock(return_value=result)
    monkeypatch.setattr(t.repo, 'can_view_portfolio', allowed)
    monkeypatch.setattr(t.repo, 'get_portfolio_status', status)
    monkeypatch.setattr(t.repo, 'get_result', read)
    yield result, connection, allowed, status, read
    t.identity.reset(token)


def test_last_rebalance_preserves_target_date_decimal_precision_and_identity(allocation_repo):
    _, connection, allowed, _, read = allocation_repo
    output = mcp_server.execute_tool('get_target_allocation', {'portfolio_id': 7}, 'advice')
    assert not output.isError
    data = output.structuredContent['data']
    assert data == {
        'portfolio_id': 7, 'name': '策略组合', 'method': 'all_risk_parity',
        'basis': 'last_rebalance', 'allocation_kind': 'strategy_target', 'weight_unit': 'decimal',
        'allocation_date': '2026-09-10', 'data_as_of_date': '2026-09-18', 'result_version': 9,
        'rebalance_band': '0.05', 'allocation_id': data['allocation_id'],
        'weights': [{'asset_key': 'A@fund', 'name': '基金 A', 'weight': '0.6'},
                    {'asset_key': 'B@fund', 'name': '基金 B', 'weight': '0.4'}],
    }
    assert len(data['allocation_id']) == 64
    assert t.get_target_allocation(7)['allocation_id'] == data['allocation_id']
    connection.execute.assert_called_with('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
    allowed.assert_called_with(connection, 7, 11, False)
    read.assert_called_with(connection, 7, None, None)


def test_latest_optimal_uses_its_own_date_and_changes_allocation_id(allocation_repo):
    result, _, _, _, _ = allocation_repo
    last = t.get_target_allocation(7)
    optimal = t.get_target_allocation(7, basis='latest_optimal', method='all_risk_parity')
    assert optimal['allocation_date'] == '2026-09-18'
    assert optimal['weights'][0]['weight'] == '0.7'
    assert optimal['allocation_id'] != last['allocation_id']
    result['portfolio']['result_version'] += 1
    assert t.get_target_allocation(7)['allocation_id'] != last['allocation_id']


@pytest.mark.parametrize('status,code,retryable', [
    ('pending', 'NOT_READY', True), ('running', 'NOT_READY', True), ('error', 'COMPUTE_FAILED', False),
])
def test_unfinished_results_are_not_exposed(allocation_repo, status, code, retryable):
    _, _, _, get_status, read = allocation_repo
    get_status.return_value = {'status': status}
    output = mcp_server.execute_tool('get_target_allocation', {'portfolio_id': 7}, 'advice')
    assert output.isError
    assert output.structuredContent['error']['code'] == code
    assert output.structuredContent['error']['retryable'] is retryable
    assert output.structuredContent['data'] is None
    read.assert_not_called()


def test_inaccessible_portfolio_stops_before_status_or_data_read(allocation_repo):
    _, _, allowed, status, read = allocation_repo
    allowed.return_value = False
    output = mcp_server.execute_tool('get_target_allocation', {'portfolio_id': 7}, 'advice')
    assert output.structuredContent['error']['code'] == 'NOT_FOUND'
    status.assert_not_called()
    read.assert_not_called()


def test_read_scope_required(allocation_repo):
    _, _, _, status, read = allocation_repo
    token = t.identity.set(AgentIdentity(11, 'compute-only', frozenset({'compute'})))
    try:
        output = mcp_server.execute_tool('get_target_allocation', {'portfolio_id': 7}, 'advice')
        assert output.structuredContent['error']['code'] == 'FORBIDDEN'
        status.assert_not_called()
        read.assert_not_called()
    finally:
        t.identity.reset(token)


@pytest.mark.parametrize('basis,missing', [
    ('last_rebalance', 'rebalances'), ('latest_optimal', 'optimal_holdings'),
])
def test_missing_basis_does_not_fall_back(allocation_repo, basis, missing):
    result, *_ = allocation_repo
    result[missing] = None
    output = mcp_server.execute_tool('get_target_allocation', {'portfolio_id': 7, 'basis': basis}, 'advice')
    assert output.structuredContent['error']['code'] == 'NOT_READY'
    assert output.structuredContent['data'] is None


@pytest.mark.parametrize('method', [None, 'all_max_sharpe'])
def test_repository_method_fallback_is_rejected(allocation_repo, method):
    result, *_ = allocation_repo
    result['portfolio']['method'] = 'all_max_sharpe'
    with pytest.raises(t.ToolError, match='所选优化方法尚无结果'):
        t.get_target_allocation(7, method=method)


@pytest.mark.parametrize('arguments', [
    {'portfolio_id': 7, 'method': 'unknown'},
    {'portfolio_id': 7, 'basis': 'actual_holdings'},
    {'portfolio_id': 7, 'account_id': 'secret'},
    {'portfolio_id': 0},
])
def test_invalid_allocation_arguments_fail_before_read(allocation_repo, arguments):
    _, _, _, _, read = allocation_repo
    output = mcp_server.execute_tool('get_target_allocation', arguments, 'advice')
    assert output.structuredContent['error']['code'] == 'INVALID_ARGUMENT'
    read.assert_not_called()


@pytest.mark.parametrize('weight', [None, float('nan'), float('inf'), '-0.1', '1.1', 'bad', '.2'])
def test_missing_invalid_or_incomplete_weights_fail_closed(allocation_repo, weight):
    result, *_ = allocation_repo
    result['rebalances'][0]['target_weights']['A@fund'] = weight
    output = mcp_server.execute_tool('get_target_allocation', {'portfolio_id': 7}, 'advice')
    assert output.structuredContent['error']['code'] == 'NOT_READY'
    assert output.structuredContent['data'] is None


def test_rounded_weights_are_returned_without_silent_renormalization(allocation_repo):
    result, *_ = allocation_repo
    result['rebalances'][0]['target_weights'] = {'A@fund': .333333, 'B@fund': .666666}
    data = t.get_target_allocation(7)
    assert [row['weight'] for row in data['weights']] == ['0.333333', '0.666666']


@pytest.mark.parametrize('field', ['data_as_of_date', 'result_version'])
def test_missing_provenance_is_not_usable_for_advice(allocation_repo, field):
    result, *_ = allocation_repo
    result['portfolio'][field] = None
    with pytest.raises(t.ToolError, match='缺少数据日期或结果版本'):
        t.get_target_allocation(7)


def test_duplicate_optimal_assets_are_rejected(allocation_repo):
    result, *_ = allocation_repo
    result['optimal_holdings']['holdings'][1]['key'] = 'A@fund'
    with pytest.raises(t.ToolError, match='重复资产'):
        t.get_target_allocation(7, basis='latest_optimal')
