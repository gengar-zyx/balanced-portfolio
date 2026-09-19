"""PostgreSQL integration tests. Set BP_MCP_TEST_DSN to a disposable database.

Each test uses its own random schema. Timescale storage operations are omitted in
this harness; the MCP migration and all business/auth SQL execute unchanged.
"""
import asyncio
import os
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import psycopg
import pyotp
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from psycopg import sql
from psycopg.types.json import Jsonb

from bp_api import agent_tools as t, agent_auth, auth, db, tasking, tasks, mcp_server
from bp_api.agent_executor import AgentExecutor
from bp_api.settings import load_settings

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def database(monkeypatch):
    dsn = os.getenv('BP_MCP_TEST_DSN')
    if not dsn:
        pytest.skip('BP_MCP_TEST_DSN not set; requires disposable PostgreSQL')
    schema = 'mcp_test_' + uuid4().hex
    admin = psycopg.connect(dsn, autocommit=True)
    admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    def connect():
        return psycopg.connect(dsn, options=f'-c search_path={schema},public')
    @contextmanager
    def get_conn():
        with connect() as conn:
            yield conn
    source = (ROOT / 'ddl/schema.sql').read_text()
    source = re.sub(r'CREATE EXTENSION[^;]+;', '', source)
    source = re.sub(r'SELECT create_hypertable\(.*?\);', '', source, flags=re.S)
    source = re.sub(r"SELECT set_chunk_time_interval\([^;]+;", '', source)
    with connect() as conn:
        conn.execute(source)
        # Migration is safe on an already initialized schema as well.
        conn.commit()
        conn.execute((ROOT / 'ddl/33_mcp_access.sql').read_text())
        conn.execute("INSERT INTO bp_user(email,password_hash,portfolio_limit) VALUES ('agent-a@test.invalid','x',100),('agent-b@test.invalid','x',100)")
        conn.commit()
    monkeypatch.setattr(db, 'get_conn', get_conn)
    monkeypatch.setenv('BP_TASK_MODE', 'inline')
    monkeypatch.setenv('REDIS_URL', '')
    class Config:
        def conninfo(self):
            return psycopg.conninfo.make_conninfo(dsn, options=f'-c search_path={schema},public')
    settings = replace(load_settings(), db=Config(), admin_initial_password=None)
    def user_id(email):
        with connect() as conn:
            return conn.execute('SELECT user_id FROM bp_user WHERE email=%s', (email,)).fetchone()[0]
    a, b = user_id('agent-a@test.invalid'), user_id('agent-b@test.invalid')
    identity = agent_auth.AgentIdentity(a, str(uuid4()), frozenset({'read','compute','portfolio:write'}), 'agent-a@test.invalid')
    token = t.identity.set(identity)
    try:
        yield SimpleNamespace(connect=connect, a=a, b=b, identity=identity, settings=settings)
    finally:
        t.identity.reset(token)
        admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        admin.close()


def payload():
    return t.CreatePortfolioIn(name='Agent test', start_date=date(2024,1,1),
        max_weight=1, risk_free_rate=.02, assets=[{'symbol':'000300','source':'cn_index_em','quadrant':'recovery'}])


def test_create_update_copy_quota_and_idempotency(database):
    first = t.create_portfolio('create-1', payload())
    assert first == t.create_portfolio('create-1', payload())
    with pytest.raises(t.ToolError, match='不同参数'):
        t.create_portfolio('create-1', payload().model_copy(update={'name':'Changed'}))
    assert t.run_backtest('run-1', first['portfolio_id'])['task_id'] == first['task_id']
    with pytest.raises(HTTPException, match='回测进行中'):
        t.update_portfolio('update-running', first['portfolio_id'], t.UpdatePortfolioIn(**payload().model_dump()))
    second = t.copy_portfolio('copy-1', first['portfolio_id'])
    assert second['portfolio_id'] != first['portfolio_id']
    with pytest.raises(t.ToolError, match='2 个'):
        t.create_portfolio('create-limit', payload())
    with database.connect() as conn:
        conn.execute("UPDATE bp_task SET status='success' WHERE task_id=%s", (first['task_id'],))
        conn.execute("UPDATE bp_portfolio SET status='done' WHERE portfolio_id=%s", (first['portfolio_id'],))
        conn.commit()
    result = t.update_portfolio('update-ok', first['portfolio_id'], t.UpdatePortfolioIn(**payload().model_dump()))
    assert result['task_id'] != first['task_id']
    assert t.update_portfolio('update-ok', first['portfolio_id'], t.UpdatePortfolioIn(**payload().model_dump())) == result


def test_parallel_retries_create_only_one_object(database):
    def run(_):
        key=t.identity.set(database.identity)
        try:
            return t.create_portfolio('same-concurrent', payload())
        finally:
            t.identity.reset(key)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(run, range(4)))
    assert len({x['portfolio_id'] for x in results}) == 1
    with database.connect() as conn:
        assert conn.execute("SELECT count(*) FROM bp_task WHERE initiated_via='mcp'").fetchone()[0] == 1


def test_user_and_admin_isolation_including_ownerless_tasks(database):
    first = t.create_portfolio('create', payload())
    with database.connect() as conn:
        ownerless = tasking.create_task(conn, 'daily_update', portfolio_id=first['portfolio_id'])
        unlinked = tasking.create_task(conn, 'ingest')
        conn.execute("UPDATE bp_user SET role='admin' WHERE user_id=%s", (database.b,))
        conn.commit()
    assert t.get_task(ownerless)['task_id']
    with pytest.raises(t.ToolError):
        t.get_task(unlinked)
    key = t.identity.set(agent_auth.AgentIdentity(database.b, 'admin-token', database.identity.scopes))
    try:
        for fn in [lambda: t.get_portfolio(first['portfolio_id']), lambda: t.get_task(first['task_id']),
                   lambda: t.get_task(ownerless), lambda: t.copy_portfolio('cross-copy', first['portfolio_id']),
                   lambda: t.run_backtest('cross-run', first['portfolio_id'])]:
            with pytest.raises((t.ToolError, HTTPException)):
                fn()
    finally:
        t.identity.reset(key)


def token_app(database):
    app=FastAPI()
    current = auth.UserContext(database.a, 'agent-a@test.invalid', 'user', False)
    app.dependency_overrides[auth.require_user] = lambda: current
    agent_auth.register_routes(app)
    return app


def test_token_lifecycle_and_totp(database):
    app=token_app(database)
    client=TestClient(app)
    r=client.post('/api/auth/agent-tokens',json={'name':'test'})
    assert r.status_code == 200
    data=r.json()
    assert r.headers['cache-control'] == 'no-store'
    assert agent_auth.authenticate_token(data['token']).user_id == database.a
    listing=client.get('/api/auth/agent-tokens').json()
    assert data['token'] not in str(listing)
    assert listing['tokens'][0]['last_used_at']
    assert auth.decode_token(data['token']) is None
    with database.connect() as conn:
        saved=conn.execute('SELECT token_hash FROM bp_agent_token').fetchone()[0]
        assert saved != data['token']
        conn.execute("UPDATE bp_user SET status='disabled' WHERE user_id=%s",(database.a,));conn.commit()
    with pytest.raises(HTTPException):
        agent_auth.authenticate_token(data['token'])
    with database.connect() as conn:
        conn.execute("UPDATE bp_user SET status='active' WHERE user_id=%s",(database.a,));conn.commit()
    app.dependency_overrides[auth.require_user] = lambda: auth.UserContext(database.b,'agent-b@test.invalid','user',False)
    assert client.delete('/api/auth/agent-tokens/'+data['token_id']).status_code==404
    app.dependency_overrides[auth.require_user] = lambda: auth.UserContext(database.a,'agent-a@test.invalid','user',False)
    assert client.delete('/api/auth/agent-tokens/'+data['token_id']).status_code==200
    with pytest.raises(HTTPException):
        agent_auth.authenticate_token(data['token'])
    secret=pyotp.random_base32()
    with database.connect() as conn:
        conn.execute('UPDATE bp_user SET totp_enabled=true,totp_secret=%s WHERE user_id=%s',(secret,database.a));conn.commit()
    assert client.post('/api/auth/agent-tokens',json={'name':'2fa'}).status_code==400
    r=client.post('/api/auth/agent-tokens',json={'name':'2fa','otp_code':pyotp.TOTP(secret).now()})
    assert r.status_code==200
    with database.connect() as conn:
        conn.execute("UPDATE bp_agent_token SET expires_at=now()-interval '1 second'");conn.commit()
    with pytest.raises(HTTPException):
        agent_auth.authenticate_token(r.json()['token'])


def test_executor_inline_recovery_and_duplicate_claim(database,monkeypatch):
    first=t.create_portfolio('create',payload())
    ran=[]
    def compute(pid, settings, tid):
        with db.get_conn() as conn:
            if not tasking.claim_compute_task(conn,tid,'test'):
                return
            tasking.mark_success(conn,tid,{'portfolio_id':pid});conn.commit()
        ran.append(tid)
    monkeypatch.setattr(tasks,'run_backtest_background',compute)
    executor=AgentExecutor(database.settings)
    assert executor.dispatch_one()
    executor.close()
    assert ran==[first['task_id']]
    with database.connect() as conn:
        assert not tasking.claim_compute_task(conn,first['task_id'],'duplicate')
        conn.commit()
    orphan=t.create_portfolio('orphan',payload())
    with database.connect() as conn:
        conn.execute("UPDATE bp_task SET execution_owner='local:dead',dispatched_at=now() WHERE task_id=%s",(orphan['task_id'],));conn.commit()
    recovery=AgentExecutor(database.settings)
    recovery.recover()
    recovery.close()
    assert t.get_task(orphan['task_id'])['status']=='failed'


def test_executor_celery_and_live_owner_not_recovered(database,monkeypatch):
    first=t.create_portfolio('create',payload())
    published=[]
    monkeypatch.setattr(tasking,'enqueue_task',lambda name, kwargs: published.append((name,kwargs)) or 'celery-test-id')
    executor=AgentExecutor(database.settings)
    assert executor.dispatch_one()
    executor.close()
    assert published[0][1]['task_id']==first['task_id']
    with database.connect() as conn:
        assert conn.execute('SELECT execution_owner FROM bp_task WHERE task_id=%s',(first['task_id'],)).fetchone()[0]=='celery'
        conn.execute("UPDATE bp_task SET execution_owner='local:alive' WHERE task_id=%s",(first['task_id'],));conn.commit()
    lease=database.connect()
    lease.execute("SELECT pg_advisory_lock(hashtextextended('local:alive',0))");lease.commit()
    recovery=AgentExecutor(database.settings)
    recovery.recover()
    assert t.get_task(first['task_id'])['status']=='queued'
    lease.close()
    recovery.recover()
    assert t.get_task(first['task_id'])['status']=='failed'
    recovery.close()


def test_otc_temporary_pricing_roundtrip(database):
    spec=t.OtcPriceIn(product_type='barrier',underlying_symbol='000300', start_date=date(2024,1,2),
        maturity_date=date(2024,2,2),s0=3000,spot=3000,vol=.2,n_paths=1000,greeks=False,
        strike_pct=100,barrier_pct=120,updown='up',inout='out',callput='call')
    created=t.price_otc('otc-1',spec)
    assert t.price_otc('otc-1',spec)==created
    executor=AgentExecutor(database.settings)
    assert executor.dispatch_one()
    executor.close()
    assert t.get_task(created['task_id'])['status']=='success'
    assert 'price' in t.get_task_result(created['task_id'])['result']
    with database.connect() as conn:
        assert conn.execute('SELECT count(*) FROM bp_otc_deal WHERE owner_user_id=%s',(database.a,)).fetchone()[0]==0


def test_sdk_uses_real_tokens_and_db(database):
    client=TestClient(token_app(database))
    token=client.post('/api/auth/agent-tokens',json={'name':'sdk','scopes':['read','compute','portfolio:write']}).json()['token']
    async def run():
        manager,endpoint=mcp_server.create_mcp()
        async with manager.run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=endpoint),headers={'Authorization':'Bearer '+token}) as http:
                async with streamable_http_client('http://localhost/mcp',http_client=http) as (read,write,_):
                    async with ClientSession(read,write) as session:
                        await session.initialize()
                        r=await session.call_tool('create_portfolio',{'request_id':'sdk-create','payload':payload().model_dump(mode='json')})
                        assert not r.isError, r
                        tid=r.structuredContent['data']['task_id']
                        task=await session.call_tool('get_task',{'task_id':tid})
                        assert task.structuredContent['data']['status']=='queued'
    asyncio.run(run())


def test_completed_real_backtest_result_sections(database,monkeypatch):
    import numpy as np
    import pandas as pd
    monkeypatch.setenv('BP_BACKTEST_METHOD_WORKERS','1')
    monkeypatch.setenv('REDIS_URL','')
    dates=pd.bdate_range('2024-01-01',periods=45)
    values=3000*np.exp(np.cumsum(np.random.default_rng(42).normal(.0002,.01,len(dates))))
    with database.connect() as conn:
        with conn.cursor() as cur:
            cur.executemany('INSERT INTO bp_quote_clean(trade_date,symbol,source,close) VALUES (%s,%s,%s,%s)',
                            [(d.date(),'000300','cn_index_em',float(v)) for d,v in zip(dates,values)])
        conn.commit()
    spec=payload().model_copy(update={'benchmark_key':'000300','lookback_days':10})
    submitted=t.create_portfolio('real-backtest',spec)
    executor=AgentExecutor(replace(database.settings,min_window=5))
    assert executor.dispatch_one()
    executor.close()
    assert t.get_task(submitted['task_id'])['status']=='success'
    summary=t.get_backtest_result(submitted['portfolio_id'])
    assert 'metrics' in summary and 'nav' not in summary
    assert len(summary['available_methods'])==4
    nav=t.get_backtest_result(submitted['portfolio_id'],section='nav',limit=3)
    assert len(nav['nav']['items'])==3 and nav['nav']['next_offset']==3
    assert t.get_task_result(submitted['task_id'])['result']['portfolio_id']==submitted['portfolio_id']


def test_expired_idempotency_key_can_be_reused(database):
    first=t.create_portfolio('expired-key',payload())
    with database.connect() as conn:
        conn.execute("UPDATE bp_agent_request SET expires_at=now()-interval '1 second'")
        conn.commit()
    second=t.create_portfolio('expired-key',payload())
    assert first['portfolio_id']!=second['portfolio_id']


def test_public_examples_and_private_otc_are_isolated(database):
    from bp_api import repositories_otc as rotc
    with database.connect() as conn:
        demo=conn.execute('SELECT portfolio_id FROM bp_portfolio WHERE is_demo=true LIMIT 1').fetchone()[0]
        private=rotc.create_otc_deal(conn,{'name':'Private OTC','product_type':'barrier','underlying_symbol':'000300'},database.b)
        public=rotc.create_otc_deal(conn,{'name':'Example OTC','product_type':'barrier','underlying_symbol':'000300'},database.b)
        conn.execute('UPDATE bp_otc_deal SET is_example=true WHERE deal_id=%s',(public,))
        conn.commit()
    assert t.get_portfolio(demo)['is_demo']
    with pytest.raises(t.ToolError):
        t.run_backtest('demo-write',demo)
    assert t.copy_portfolio('demo-copy',demo)['portfolio_id'] != demo
    assert t.get_otc_deal(public)['name']=='Example OTC'
    with pytest.raises(t.ToolError):
        t.get_otc_deal(private)
    assert private not in {item['deal_id'] for item in t.list_otc_deals()['items']}


def test_rest_recompute_does_not_redispatch_mcp_task(database, monkeypatch):
    from fastapi import BackgroundTasks
    from bp_api import main
    submitted=t.create_portfolio('mcp-create',payload())
    published=[]
    monkeypatch.setattr(main,'_dispatch_backtest',lambda *a: published.append(a))
    result=main.recompute(submitted['portfolio_id'],BackgroundTasks(),database.identity.user)
    assert result['task_id']==submitted['task_id']
    assert '_reused_task' not in result
    assert not published
