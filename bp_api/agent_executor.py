"""Durable MCP dispatch plus a lifespan-owned, bounded inline executor.

A session advisory lock is a process lease: other API processes only recover local
jobs after PostgreSQL confirms that the owning connection has disappeared.
"""
from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg

from . import db, tasking, tasks

log = logging.getLogger(__name__)


class AgentExecutor:
    def __init__(self, settings):
        self.settings = settings
        self.owner = 'local:' + str(uuid.uuid4())
        self.stop_event = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='bp-agent')
        self.lease = None
        self.thread = None

    def start(self):
        self.lease = psycopg.connect(self.settings.db.conninfo(), autocommit=True)
        self.lease.execute('SELECT pg_advisory_lock(hashtextextended(%s, 0))', (self.owner,))
        self.recover()
        self.thread = threading.Thread(target=self._loop, name='bp-agent-dispatch', daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()
        self.pool.shutdown(wait=True)
        if self.lease:
            self.lease.close()

    def recover(self):
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM bp_agent_request WHERE expires_at<=now()")
                cur.execute("""SELECT DISTINCT execution_owner FROM bp_task
                    WHERE execution_owner LIKE 'local:%%' AND status IN ('queued','running')""")
                owners = [r[0] for r in cur.fetchall()]
                for owner in owners:
                    cur.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))', (owner,))
                    if not cur.fetchone()[0]:
                        continue
                    cur.execute("""UPDATE bp_task SET status='failed', error='本地执行已中断，请重新提交',
                        progress_message='本地执行已中断', finished_at=now()
                        WHERE execution_owner=%s AND status IN ('queued','running') RETURNING portfolio_id""", (owner,))
                    pids = [r[0] for r in cur.fetchall() if r[0] is not None]
                    if pids:
                        cur.execute("""UPDATE bp_portfolio SET status='error', error='本地执行已中断，请重算'
                            WHERE portfolio_id=ANY(%s) AND status='running'""", (pids,))
            conn.commit()

    def dispatch_one(self):
        # Commit the dispatch claim before submitting to our local executor.
        local = None
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT task_id, dispatch_payload FROM bp_task
                    WHERE initiated_via='mcp' AND status='queued' AND dispatched_at IS NULL
                    ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1""")
                row = cur.fetchone()
                if not row:
                    return False
                tid, payload = str(row[0]), row[1]
                celery_id = tasking.enqueue_task(payload['task_name'], payload['kwargs'])
                owner = 'celery' if celery_id else self.owner
                cur.execute('''UPDATE bp_task SET dispatched_at=now(), execution_owner=%s,
                    celery_id=%s WHERE task_id=%s''', (owner, celery_id, tid))
                if not celery_id:
                    local = payload
            conn.commit()
        if local:
            self.pool.submit(self._run, local)
        return True

    def _run(self, payload):
        kwargs = payload['kwargs']
        try:
            if payload['task_name'] == 'bp_api.backtest':
                tasks.run_backtest_background(kwargs['portfolio_id'], self.settings, kwargs['task_id'])
            else:
                tasks.run_otc_price_background(kwargs['spec'], None, kwargs['task_id'])
        except Exception:
            log.exception('Local execution failed task_id=%s', kwargs['task_id'])
            with db.get_conn() as conn:
                tasking.mark_failed(conn, kwargs['task_id'], '本地执行失败')
                conn.commit()

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                # Fail closed if this process loses its recovery lease.
                self.lease.execute('SELECT 1')
                self.recover()
                while not self.stop_event.is_set() and self.dispatch_one():
                    pass
            except Exception:
                log.exception('MCP dispatch cycle failed')
            self.stop_event.wait(2)
