"""Protocol and projection tests; no live market data or production credentials."""
import asyncio
import json
from datetime import date
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from bp_api import agent_tools as t, mcp_server, crypto, cffex, repositories as repo
from bp_api.agent_auth import AgentIdentity, token_hash


@pytest.fixture
def agent():
    value = AgentIdentity(11, str(uuid4()), frozenset({'read', 'portfolio:write', 'compute'}))
    key = t.identity.set(value)
    yield value
    t.identity.reset(key)


def test_all_tool_schemas_are_strict_and_scope_limited():
    assert len(t.TOOLS) == 21
    for tool in t.TOOLS.values():
        schema = tool.model.model_json_schema()
        assert schema['additionalProperties'] is False
        if tool.scopes != frozenset({'read'}):
            assert 'request_id' in schema['required']
    assert 'delete_portfolio' not in t.TOOLS


def test_invalid_inputs_and_forbidden_are_structured(agent):
    result = mcp_server.execute_tool('list_assets', {'limit': 501}, 'r1')
    assert result.isError and result.structuredContent['error']['code'] == 'INVALID_ARGUMENT'
    result = mcp_server.execute_tool('list_assets', {'owner_user_id': 55}, 'r2')
    assert result.isError
    key = t.identity.set(AgentIdentity(11, 'token', frozenset({'read'})))
    try:
        result = mcp_server.execute_tool('run_backtest', {'request_id': 'abc', 'portfolio_id': 1}, 'r3')
        assert result.structuredContent['error']['code'] == 'FORBIDDEN'
    finally:
        t.identity.reset(key)


def test_json_nonfinite_and_secret_hash():
    assert mcp_server.json_safe({'nan': float('nan'), 'inf': float('inf'), 'date': date(2024, 1, 1)}) == {
        'nan': None, 'inf': None, 'date': '2024-01-01'}
    assert token_hash('a') != token_hash('b')
    assert len(token_hash('a')) == 64


def test_text_only_clients_receive_complete_results(monkeypatch, agent):
    payload = {'items': [{'name': '沪深300', 'value': float('nan')}],
               'total': 2, 'next_offset': 1, 'as_of': date(2024, 1, 2)}
    monkeypatch.setattr(t.TOOLS['get_capabilities'], 'fn', lambda: payload)
    result = mcp_server.execute_tool('get_capabilities', {}, 'text-client')
    decoded = json.loads(result.content[0].text.split('\n', 1)[1])
    assert decoded == result.structuredContent
    assert decoded['data']['items'][0] == {'name': '沪深300', 'value': None}
    assert decoded['data']['next_offset'] == 1
    invalid = mcp_server.execute_tool('list_assets', {'limit': 501}, 'text-error')
    assert invalid.isError
    assert json.loads(invalid.content[0].text.split('\n', 1)[1]) == invalid.structuredContent


def test_cffex_pagination_preserves_values(monkeypatch, agent):
    monkeypatch.setattr(cffex, '_handle_history', lambda *a, **k: {
        'dates': ['2024-01-01', '2024-01-02', '2024-01-03'],
        'series': {'IF': {'ann_premium_rates': [.1, None, .3], 'index_prices': [10, 20, 30]}}})
    result = t.get_cffex_history(start_date=date(2024, 1, 2), limit=1)
    assert result['total'] == 2 and result['next_offset'] == 1
    assert result['items'] == [{'trade_date': '2024-01-02', 'series': {'IF': {'ann_premium_rate': None, 'index_price': 20}}}]


def test_crypto_pagination_preserves_date_axis(monkeypatch, agent):
    monkeypatch.setattr(crypto, 'get_correlation_payload', lambda: {
        'is_ready': True, 'meta': {'calendar': 'NYSE', 'version': 4},
        'dates': ['2024-01-01', '2024-01-02', '2024-01-03'], 'btc_prices': [1, 2, 3],
        'rolling': {'3M': {'pearson': {'sp500': {'correlation': [None, .2, .3]}}}},
        'lagged_shifted': {'3M': {'dates': ['2024-01-01', '2024-01-02'], 'btc': [1, 2], 'dxy': [3, 4]}}})
    r = t.get_crypto_correlation(section='rolling', asset='sp500', limit=1, offset=1)
    assert r['items'][0] == {'trade_date': '2024-01-02', 'btc_price': 2, 'correlations': {'sp500': .2}}
    assert r['meta']['version'] == 4
    r = t.get_crypto_correlation(section='lagged', offset=1)
    assert r['items'][0]['dxy'] == 4
    with pytest.raises(t.ToolError):
        t.get_crypto_correlation(asset='fake')


def test_errors_do_not_leak_exception_details(monkeypatch, agent):
    def fail():
        raise RuntimeError('postgres://private:secret@database')
    monkeypatch.setattr(t.TOOLS['get_capabilities'], 'fn', fail)
    r = mcp_server.execute_tool('get_capabilities', {}, 'test-error')
    assert r.isError
    assert 'secret' not in str(r.structuredContent)
    assert 'secret' not in r.content[0].text
    assert r.structuredContent['request_id'] == 'test-error'


def test_protocol_sdk_roundtrip_and_auth_isolation(monkeypatch):
    def authenticate(token):
        if token == 'read-token':
            return AgentIdentity(1, 'read', frozenset({'read'}))
        if token == 'write-token':
            return AgentIdentity(2, 'write', frozenset({'read', 'compute', 'portfolio:write'}))
        raise HTTPException(401)
    monkeypatch.setattr(mcp_server, 'authenticate_token', authenticate)
    monkeypatch.setattr(t.TOOLS['get_capabilities'], 'fn', lambda: {'user_id': t.identity.get().user_id})

    async def run():
        manager, endpoint = mcp_server.create_mcp()
        transport = httpx.ASGITransport(app=endpoint)
        async with manager.run():
            async with httpx.AsyncClient(transport=transport, base_url='http://localhost') as client:
                assert (await client.post('/mcp')).status_code == 401
                assert (await client.post('/mcp', headers={'Authorization': 'Bearer bad'})).status_code == 401
            async def check(token, uid, write):
                async with httpx.AsyncClient(transport=transport, headers={'Authorization': 'Bearer ' + token}) as client:
                    async with streamable_http_client('http://localhost/mcp', http_client=client) as (read, write_stream, _):
                        async with ClientSession(read, write_stream) as session:
                            await session.initialize()
                            listing = await session.list_tools()
                            assert ('price_otc' in {x.name for x in listing.tools}) == write
                            result = await session.call_tool('get_capabilities')
                            assert not result.isError
                            assert result.structuredContent['data']['user_id'] == uid
                            denied = await session.call_tool('list_assets', {'limit': 0})
                            assert denied.isError
            await asyncio.gather(check('read-token', 1, False), check('write-token', 2, True))
            async with httpx.AsyncClient(transport=transport) as client:
                response = await client.post('http://evil.example/mcp', headers={'Authorization': 'Bearer read-token', 'Content-Type':'application/json'})
                assert response.status_code == 421
                response = await client.post('http://localhost/mcp', headers={'Authorization': 'Bearer read-token', 'Origin': 'https://evil.example', 'Content-Type':'application/json'})
                assert response.status_code == 403
    asyncio.run(run())


def test_otc_chart_projection_and_dates():
    result={'price':100,'greeks':{'delta':.1},'chart':{
        'dates':['2024-01-01','2024-01-02'],'underlying':[10,20],'pnl':[1,2],
        'baseline_line':[10,10],'ko_line':12,'events':[{'date':'2024-01-02','type':'knock_out'}],
        'ko_observation_dates':['2024-01-02']}}
    assert 'chart' not in t.otc_result_view(result,'summary')
    r=t.otc_result_view(result,'chart',start_date=date(2024,1,2),limit=1)
    assert r['items']==[{'trade_date':'2024-01-02','underlying':20,'pnl':2,'baseline_line':10}]
    assert r['meta']['ko_line']==12
    assert len(t.otc_result_view(result,'events')['items'])==1


def test_crypto_missing_day_does_not_shift_series():
    class Cursor:
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def execute(self,*a): pass
        def fetchall(self):
            return [(date(2024,1,1),'sp500','pearson',.1,.2,.3,.4),
                    (date(2024,1,3),'sp500','pearson',.5,.6,.7,.8)]
    class Conn:
        def cursor(self): return Cursor()
    data=crypto._read_rolling(Conn(),['2024-01-01','2024-01-02','2024-01-03'])
    assert data['3M']['pearson']['sp500']['correlation']==[.1,None,.5]


def test_otc_rejects_unknown_fields_and_unbounded_steps():
    from pydantic import ValidationError
    data={'product_type':'barrier','underlying_symbol':'000300','start_date':'2024-01-01',
          'maturity_date':'2024-02-01','s0':3000,'vol':.2}
    with pytest.raises(ValidationError):
        t.OtcPriceIn(**data,deal_id=1)
    with pytest.raises(ValidationError):
        t.OtcPriceIn(**data,t_step_per_year=100000)
