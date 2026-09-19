"""Official MCP SDK Streamable HTTP adapter with per-request PAT authentication."""
from __future__ import annotations

import json
import logging
import math
import os
import time
import uuid
from datetime import date, datetime
from decimal import Decimal

import anyio
from fastapi import HTTPException
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import agent_tools
from .agent_auth import authenticate_token

log = logging.getLogger('bp_api.mcp')


def json_safe(value):
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (date, datetime, uuid.UUID)):
        return str(value)
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def execute_tool(name, arguments, request_id):
    agent = agent_tools.identity.get()
    start = time.monotonic()
    error = None
    data = None
    try:
        definition = agent_tools.TOOLS.get(name)
        if not definition:
            raise agent_tools.ToolError('NOT_FOUND', '未知工具')
        if not definition.scopes.issubset(agent.scopes):
            raise agent_tools.ToolError('FORBIDDEN', '令牌权限不足')
        args = definition.model.model_validate(arguments or {})
        data = definition.fn(**{k: getattr(args, k) for k in type(args).model_fields})
    except ValidationError as exc:
        details = '; '.join('.'.join(map(str, e['loc'])) + ': ' + e['msg'] for e in exc.errors(include_input=False, include_url=False))
        error = agent_tools.ErrorInfo(code='INVALID_ARGUMENT', message=details)
    except agent_tools.ToolError as exc:
        error = agent_tools.ErrorInfo(code=exc.code, message=exc.message, retryable=exc.retryable)
    except HTTPException as exc:
        codes = {400: 'INVALID_ARGUMENT', 401: 'UNAUTHORIZED', 403: 'FORBIDDEN', 404: 'NOT_FOUND', 409: 'CONFLICT', 422: 'INVALID_ARGUMENT', 429: 'COMPUTE_LIMIT'}
        error = agent_tools.ErrorInfo(code=codes.get(exc.status_code, 'INTERNAL_ERROR'),
            message=str(exc.detail) if exc.status_code < 500 else '服务暂不可用，请携带请求 ID 排查', retryable=exc.status_code in (409, 429, 503))
    except KeyError:
        error = agent_tools.ErrorInfo(code='NOT_FOUND', message='对象不存在或数据尚未就绪')
    except Exception:
        log.exception('Tool failed request_id=%s tool=%s', request_id, name)
        error = agent_tools.ErrorInfo(code='INTERNAL_ERROR', message='服务暂不可用，请携带请求 ID 排查', retryable=True)
    target = {k: (arguments or {}).get(k) for k in ('portfolio_id', 'deal_id', 'task_id') if isinstance((arguments or {}).get(k), (int, str))}
    log.info('tool=%s user=%s token_id=%s request_id=%s target=%s task_id=%s elapsed_ms=%d error=%s',
             name, agent.user_id, agent.token_id, request_id, target,
             data.get('task_id') if isinstance(data, dict) else None,
             (time.monotonic() - start) * 1000, error.code if error else None)
    output = agent_tools.ToolOutput(request_id=request_id, data=json_safe(data), error=error)
    structured = output.model_dump(mode='json')
    summary = f'{name}: {error.message}' if error else f'{name}: 完成。'
    # Some clients only forward text content to the model. Keep the complete,
    # sanitized result in the same text block so no separate data channel is required.
    text = summary + '\n' + json.dumps(structured, ensure_ascii=False, allow_nan=False)
    return types.CallToolResult(content=[types.TextContent(type='text', text=text)],
                                structuredContent=structured, isError=error is not None)


def create_mcp():
    server = Server('Balanced Portfolio', version='1.0.0', instructions=(
        'Use get_capabilities to discover supported parameters. Read portfolio configuration before a full update. '
        'Writes require a stable request_id: retry with identical arguments. Computations return task_id; '
        'poll get_task at least 2 seconds apart, then request results. All rates are decimals except *_pct fields. '
        'Access is restricted to your objects and public examples, regardless of administrator role.'))

    @server.list_tools()
    async def list_tools():
        agent = agent_tools.identity.get()
        return [types.Tool(name=d.name, description=d.description, inputSchema=d.model.model_json_schema(),
                           outputSchema=agent_tools.ToolOutput.model_json_schema(),
                           annotations=types.ToolAnnotations(readOnlyHint=d.scopes == frozenset({'read'}),
                                                            destructiveHint=False, idempotentHint=True, openWorldHint=False))
                for d in agent_tools.TOOLS.values() if d.scopes.issubset(agent.scopes)]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        return await anyio.to_thread.run_sync(execute_tool, name, arguments, str(uuid.uuid4()))

    hosts = [x.strip() for x in os.getenv('BP_MCP_ALLOWED_HOSTS', 'localhost,localhost:*,127.0.0.1,127.0.0.1:*').split(',') if x.strip()]
    origins = [x.strip() for x in os.getenv('BP_MCP_ALLOWED_ORIGINS', 'http://localhost:3000,http://127.0.0.1:3000').split(',') if x.strip()]
    if not hosts or any(x == '*' for x in hosts + origins):
        raise RuntimeError('MCP requires explicit Host/Origin allowlists')
    manager = StreamableHTTPSessionManager(server, json_response=True, stateless=True,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                     allowed_hosts=hosts, allowed_origins=origins))

    async def endpoint(scope, receive, send):
        request = Request(scope, receive)
        authorization = request.headers.get('authorization', '')
        if not authorization.lower().startswith('bearer '):
            await JSONResponse({'error': 'Agent Bearer token required'}, status_code=401,
                               headers={'WWW-Authenticate': 'Bearer', 'Cache-Control': 'no-store'})(scope, receive, send)
            return
        try:
            agent = await anyio.to_thread.run_sync(authenticate_token, authorization[7:].strip())
        except HTTPException:
            await JSONResponse({'error': 'Agent token invalid or expired'}, status_code=401,
                               headers={'WWW-Authenticate': 'Bearer', 'Cache-Control': 'no-store'})(scope, receive, send)
            return
        except Exception:
            rid = str(uuid.uuid4())
            log.exception('MCP authentication unavailable request_id=%s', rid)
            await JSONResponse({'error': 'Authentication unavailable', 'request_id': rid}, status_code=503)(scope, receive, send)
            return
        context = agent_tools.identity.set(agent)
        async def no_cache_send(message):
            if message['type'] == 'http.response.start':
                message.setdefault('headers', []).append((b'cache-control', b'no-store'))
            await send(message)
        try:
            await manager.handle_request(scope, receive, no_cache_send)
        finally:
            agent_tools.identity.reset(context)
    return manager, endpoint
