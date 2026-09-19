"""Revocable personal access tokens. These credentials never authenticate REST calls."""
from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from typing import Literal

import pyotp
from fastapi import Depends, HTTPException, Response
from pydantic import BaseModel, Field

from . import auth, db

Scope = Literal['read', 'portfolio:write', 'compute']


class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[Scope] = Field(default_factory=lambda: ['read'], min_length=1)
    expires_days: Literal[30, 90, 365] = 90
    otp_code: str | None = Field(default=None, max_length=6)


@dataclass(frozen=True)
class AgentIdentity:
    user_id: int
    token_id: str
    scopes: frozenset[str]
    email: str = ''

    @property
    def user(self):
        # Deliberately do not propagate administrator privileges to MCP.
        return auth.UserContext(self.user_id, self.email, 'user', False)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def authenticate_token(token: str) -> AgentIdentity:
    if not token.startswith('bp_agent_') or len(token) > 200:
        raise HTTPException(401, 'Agent 令牌无效或已过期')
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute('''UPDATE bp_agent_token t SET last_used_at=now()
                FROM bp_user u WHERE t.owner_user_id=u.user_id AND t.token_hash=%s
                AND t.revoked_at IS NULL AND t.expires_at>now() AND u.status='active'
                RETURNING u.user_id, t.token_id, t.scopes, u.email''', (token_hash(token),))
            row = cur.fetchone()
        conn.commit()
    if not row:
        raise HTTPException(401, 'Agent 令牌无效或已过期')
    return AgentIdentity(row[0], str(row[1]), frozenset(row[2]), row[3])


def require_identity(user):
    if user.user_id is None:
        raise HTTPException(401, '请使用有效的平台用户')
    return user.user_id


def register_routes(app):
    @app.get('/api/auth/agent-tokens')
    def list_tokens(response: Response, user=Depends(auth.require_user)):
        uid = require_identity(user)
        response.headers['Cache-Control'] = 'no-store'
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('''SELECT token_id, name, prefix, scopes, created_at,
                    expires_at, last_used_at, revoked_at FROM bp_agent_token
                    WHERE owner_user_id=%s ORDER BY created_at DESC''', (uid,))
                names = ['token_id', 'name', 'prefix', 'scopes', 'created_at',
                         'expires_at', 'last_used_at', 'revoked_at']
                return {'tokens': [dict(zip(names, r)) for r in cur.fetchall()]}

    @app.post('/api/auth/agent-tokens')
    def create_token(payload: TokenCreate, response: Response, user=Depends(auth.require_user)):
        uid = require_identity(user)
        response.headers['Cache-Control'] = 'no-store'
        if not payload.name.strip():
            raise HTTPException(400, '令牌名称不能为空')
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT totp_enabled, totp_secret FROM bp_user WHERE user_id=%s AND status=\'active\' FOR UPDATE', (uid,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(401, '用户不可用')
                if user.is_admin and not row[0]:
                    raise HTTPException(403, '管理员须先绑定两步验证')
                if row[0] and not (row[1] and payload.otp_code and pyotp.TOTP(row[1]).verify(payload.otp_code, valid_window=1)):
                    raise HTTPException(400, '请输入正确的两步验证码')
                token = 'bp_agent_' + secrets.token_urlsafe(32)
                tid = str(uuid.uuid4())
                cur.execute('''INSERT INTO bp_agent_token
                    (token_id, owner_user_id, name, token_hash, prefix, scopes, expires_at)
                    VALUES (%s,%s,%s,%s,%s,%s,now() + %s * interval '1 day')
                    RETURNING expires_at''',
                    (tid, uid, payload.name.strip(), token_hash(token), token[:17],
                     sorted(set(payload.scopes)), payload.expires_days))
                expires = cur.fetchone()[0]
            conn.commit()
        return {'token_id': tid, 'token': token, 'expires_at': expires}

    @app.delete('/api/auth/agent-tokens/{token_id}')
    def revoke_token(token_id: uuid.UUID, response: Response, user=Depends(auth.require_user)):
        uid = require_identity(user)
        response.headers['Cache-Control'] = 'no-store'
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('''UPDATE bp_agent_token SET revoked_at=COALESCE(revoked_at, now())
                    WHERE token_id=%s AND owner_user_id=%s RETURNING token_id''', (token_id, uid))
                if not cur.fetchone():
                    raise HTTPException(404, '令牌不存在')
            conn.commit()
        return {'ok': True}
