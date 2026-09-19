# Agent MCP 接入

服务提供官方 Python MCP SDK 的 Streamable HTTP 入口 `/mcp`，与 FastAPI 共用进程、业务服务、数据库和计算引擎。使用无状态 HTTP 和 JSON 响应，无须维持 MCP session；计算任务状态保存在 PostgreSQL。

第一版适用于可以手动配置 HTTP 地址及 Bearer Token 的客户端。令牌是本站专用的个人访问令牌，不是浏览器 JWT；不支持 OAuth 自动登录发现、旧 HTTP+SSE 或 stdio 传输。

## 部署与启用

1. 安装依赖：`pip install -r requirements.txt`。MCP SDK 固定为已验证的 `1.29.1`。
2. **已有数据库**在启用前执行 `psql -v ON_ERROR_STOP=1 -f ddl/33_mcp_access.sql`。**全新数据库**只执行 `ddl/schema.sql`。部署脚本不会自动运行迁移。
3. 在服务端环境中设置：

   ```dotenv
   BP_MCP_ENABLED=true
   BP_MCP_ALLOWED_HOSTS=192.168.50.86:3001,192.168.50.86,localhost,localhost:*,127.0.0.1,127.0.0.1:*
   BP_MCP_ALLOWED_ORIGINS=http://192.168.50.86:3001,http://localhost:3000,http://127.0.0.1:3000
   ```

   Host 不含协议；如使用非默认端口，显式写入 `host:port`。本地默认允许 `localhost`、`127.0.0.1` 及其端口。Origin 是浏览器页面来源，可逗号分隔；没有 Origin 的原生客户端仍需通过 Host 和令牌验证。不要配置全局 `*`。

   上述配置对应现有局域网入口 `http://192.168.50.86:3001`，保持原有端口与代理拓扑。同时允许不带端口的 IP，是为了兼容 Nginx 的 `proxy_set_header Host $host`；保留本机地址供 Next.js 转发使用。Host 白名单检查服务访问地址，不限制客户端设备 IP。通用 `.env.example` 仍默认关闭 MCP；本机 `.env` 不入 git，发布代码时需将这三项同步到服务器 `.env`。

4. 采用更新后的 Nginx 模板：`location = /mcp` 转发 FastAPI，禁用缓冲和缓存。生产环境通过 HTTPS 暴露服务。保持 `/api/session*` 转发 Next.js。
5. 同步升级并重启 API 和 Celery worker；worker 的计算任务领取是原子的，防止重复投递或队列发送结果不确定时重复计算。
6. 用户登录网站后进入导航栏的 **Agent 访问设置**（`/settings/agent-access`），创建并复制令牌。

   必须先部署包含 MCP 的代码和依赖、确认迁移 33 已执行，再加载服务器 `.env` 并重启 API。若使用现有 PM2 部署，在项目根目录执行 `set -a; source .env; set +a`，随后执行 `pm2 restart deploy/ecosystem.config.cjs --update-env`。如果通过 Next.js rewrite 提供 `/mcp`，首次升级还需重新构建前端。

   验证 `http://192.168.50.86:3001/mcp`：无令牌请求应返回 HTTP 401（404 表示入口尚未生效）；带网页创建的令牌，通过 MCP SDK 完成 `initialize()` 和 `list_tools()`。后文完整调用示例的 `MCP_URL` 使用该地址。

`BP_MCP_ENABLED` 默认为 `false`，关闭时不注册 MCP 和令牌管理接口，不启动 MCP 派发线程，也不要求旧数据库预先迁移。关闭后令牌仍保留在数据库中；重新开启时未过期且未撤销的令牌恢复可用。回滚前先等待正在计算的任务结束；数据库新增结构可以保留。

开发时 Next.js 的 `/mcp` rewrite 也会转发到 `BP_API_BASE`。生产建议按 Nginx 模板直接转发 API。

### 现有 Mac mini Docker 部署

`192.168.50.86:3001` 使用 Docker Compose：宿主机 `3001` 映射至 Next.js `3000`，Next.js 再转发至 `http://api:8000`。此部署的 `BP_MCP_ALLOWED_HOSTS` 还须追加 `api:8000`，允许 rewrite 实际使用的内部 Host；客户端地址仍为 `http://192.168.50.86:3001/mcp`。

升级时先备份数据库和服务器 `.env`，构建 API、worker、beat 与 web 镜像，执行迁移 33，再以原 Compose 项目名重建这些服务。只执行 `docker compose restart` 不会加载修改后的 `.env`，应使用 `docker compose up -d --no-build api worker beat web`。保留原数据库和 Redis 数据卷，勿执行 `down -v`。先验证无令牌返回 401，再用 SDK 验证初始化、工具发现、临时定价与令牌撤销。

## 令牌与权限

- 令牌使用 256 位随机密钥，前缀为 `bp_agent_`；明文只在创建响应中出现一次，仅在当前页面内存中展示。数据库存 SHA-256 哈希和识别前缀。
- 有效期可选 30、90、365 天，默认 90 天。创建令牌时，已绑定 TOTP 的用户需要输入动态码；管理员须先完成 TOTP 绑定。
- 创建、列出、撤销接口分别为 `POST/GET /api/auth/agent-tokens`、`DELETE /api/auth/agent-tokens/{token_id}`，通过现有网站登录 JWT 鉴权，仅操作当前用户的令牌。
- 令牌撤销、过期或用户禁用后，下一次 MCP HTTP 请求立即失效。令牌不能用于 REST 登录或管理接口。
- MCP 身份不会继承管理员的全局访问权。只能查看自己的组合/合约及公开示例；只能修改自己的非示例组合。

| Scope | 能力 |
| --- | --- |
| `read` | 查询资产、市场数据、组合、合约和计算结果；默认勾选 |
| `portfolio:write` + `compute` | 创建、更新和复制组合（自动回测） |
| `compute` | 重算自己的组合、提交临时 OTC 定价 |

建议需要计算的客户端同时授予 `read`，方便查询进度和读取结果。工具发现按权限过滤，每次实际调用仍会再次检查权限。

## 客户端配置

在客户端的远程 MCP 配置中填写：

```json
{
  "url": "http://192.168.50.86:3001/mcp",
  "headers": {
    "Authorization": "Bearer <在网页创建的令牌>"
  }
}
```

这是地址和请求头示例；外围配置格式取决于客户端。令牌应使用客户端的密钥存储或环境变量，不提交到代码仓库。若客户端只支持 OAuth 授权，此版本无法直接连接。

## 工具目录

| 业务 | 工具 |
| --- | --- |
| 能力发现 | `get_capabilities` |
| 资产 | `list_assets` |
| 组合 | `list_portfolios`, `get_portfolio`, `create_portfolio`, `update_portfolio`, `copy_portfolio`, `run_backtest` |
| 回测结果 | `get_backtest_result` |
| CFFEX | `get_cffex_snapshot`, `get_cffex_history`, `get_cffex_statistics` |
| 加密货币 | `get_crypto_correlation` |
| OTC 参数 | `list_otc_underlyings`, `get_otc_market_inputs`, `get_otc_observation_dates` |
| 已存合约 | `list_otc_deals`, `get_otc_deal` |
| 临时定价 | `price_otc` |
| 任务 | `get_task`, `get_task_result` |

具体参数与 JSON Schema 通过 `tools/list` 获取。输入拒绝未知工具参数。资产标识始终为 `symbol@source`；利率、收益率及波动率使用小数（`0.02 = 2%`），OTC `*_pct` 障碍字段使用百分数（`103 = 103%`）。

`get_capabilities` 返回支持的优化方法、基准、象限、OTC 产品/引擎组合和加密货币筛选键。MCP 不开放尚未实际实现的 PDE；雪球和凤凰使用 MC，障碍和气囊可选 MC、analytic、quad。MCP 定价保留现有路径数、随机种子等默认值，计算步数限制为每年 1–366，合约期限最多 30 年。

`get_otc_market_inputs(on_date=...)` 的日期只用于历史点位；返回的 realized volatility 是最新可用估计，不能当作该历史日期的波动率。结果会明确标注这一口径。

### 结果结构与分页

工具返回 `structuredContent`：

```json
{
  "request_id": "服务端日志请求 ID",
  "data": {"task_id": "任务 UUID", "status": "queued", "poll_after_seconds": 2},
  "error": null
}
```

另附简短文本摘要。业务错误设置 MCP `isError=true`，并返回 `error.code/message/retryable`；内部异常只向客户端给出通用描述和请求 ID。错误码包括 `INVALID_ARGUMENT`、`NOT_FOUND`、`FORBIDDEN`、`UNAUTHORIZED`、`CONFLICT`、`NOT_READY`、`COMPUTE_LIMIT`、`COMPUTE_FAILED` 和 `INTERNAL_ERROR`。

- 列表及时间序列默认 `limit=100, offset=0`，最大 `limit=500`；返回 `items/total/next_offset`。大序列支持 `start_date/end_date`。
- 回测默认 `section="summary"`，包含指标、持仓、方法、组合参数及数据截止日。按需读取 `nav/rebalances/corr/attribution`，归因中的列表独立分页。
- OTC 任务结果默认返回价格、Greeks、状态和模型元数据；`available_sections` 指示可读取的 `chart/events/observation_dates` 等分区。已存合约也支持这些分区。
- 加密货币默认返回收盘快照和选定方法/窗口在有效交易日的相关系数；滚动和 BTC/DXY 平移序列按原始日期轴分页。CFFEX 保持同交易日收盘确认规则。
- 金融计算的 NaN/Infinity 统一转成 JSON `null`。缺失行情和未就绪数据不会伪装成零值。

## 完整 SDK 示例

安装项目依赖，将令牌放到环境变量 `BP_AGENT_TOKEN`。以下脚本创建一个单资产示例组合；运行前先确认该资产在自己的服务中可用且有足够历史数据。

```python
import asyncio
import os
import uuid

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    headers = {"Authorization": f"Bearer {os.environ['BP_AGENT_TOKEN']}"}
    async with httpx.AsyncClient(headers=headers, timeout=60) as http:
        async with streamable_http_client(
            "https://your-domain.example/mcp", http_client=http
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                async def call(name, arguments=None):
                    result = await session.call_tool(name, arguments or {})
                    value = result.structuredContent
                    if result.isError:
                        raise RuntimeError(value)
                    return value["data"]

                await call("get_capabilities")
                await call("list_assets", {"keyword": "000300"})
                # Store and reuse this ID if the submission response is lost.
                request_id = str(uuid.uuid4())
                created = await call("create_portfolio", {
                    "request_id": request_id,
                    "payload": {
                        "name": "Agent 示例",
                        "start_date": "2020-01-01",
                        "method": "all_risk_parity",
                        "ratio": "sharpe",
                        "risk_free_rate": 0.02,
                        "max_weight": 1.0,
                        "benchmark_key": "000300",
                        "assets": [{
                            "symbol": "000300", "source": "cn_index_em",
                            "quadrant": "recovery"
                        }]
                    }
                })
                while True:
                    task = await call("get_task", {"task_id": created["task_id"]})
                    if task["status"] in ("success", "failed", "cancelled"):
                        break
                    await asyncio.sleep(task["poll_after_seconds"])
                if task["status"] != "success":
                    raise RuntimeError(task)
                summary = await call("get_backtest_result", {
                    "portfolio_id": created["portfolio_id"]
                })
                print(summary["metrics"])

asyncio.run(main())
```

更新组合使用完整配置替换，先调用 `get_portfolio`，仅把创建/更新模型支持的字段传给 `payload`。不要原样传回结果中的状态、所有者或只读字段。OTC `price_otc` 不接受 `deal_id`，只创建计算任务，不写入保存的合约。

## 派发与运行维护

- 提交事务同时保存对象、任务、待派发参数及幂等响应；派发线程提交事务后再进行 Celery 发布或本地执行。
- `request_id` 在用户和工具范围内唯一，保留 24 小时；相同参数返回原提交响应，不同参数返回冲突。返回的是原提交时状态，最新状态使用 `get_task`。
- 每用户最多同时存在两个由 MCP 发起的未完成计算任务。组合写入与并发限额检查在用户锁下串行；组合编辑另持有组合行锁。
- Celery 优先；`BP_TASK_MODE=inline` 或发布失败时降级到每个 API 进程一个本地执行线程。未派发记录可在重启后继续派发。
- 本地任务的进程归属由独立 PostgreSQL 会话锁保护。其他进程只有确认原锁已释放，才会将中断任务标记失败；不会误回收仍持锁的其他 API 进程。
- 优雅关闭等待已交给本地执行器的任务结束。PM2 默认强制终止等待时间通常不足以完成量化任务；生产优先使用 Celery，需要等待本地任务时配置合适的 PM2 `kill_timeout`。被强制中断的本地任务下次启动会失败并允许重新提交。
- Celery 发布成功但 worker 不在线时，任务仍会排队；运行维护仍需监控原有 Celery/Redis。分派进程的数据库会话锁连接若断开，会停止新的派发并记录日志，需恢复数据库连接后重启 API；不会擅自把所有任务认作可重试。
- 审计日志包含工具名、用户 ID、令牌 ID、目标对象、任务 ID、请求 ID、耗时和错误码，不记录 Bearer Token。`get_task` 不返回内部 Celery ID 或原始异常详情。

## 测试

```bash
python -m pytest bp_api/tests -q
cd web
npm run build
```

数据库集成测试需要显式配置**可用于测试的 PostgreSQL**：

```bash
BP_MCP_TEST_DSN='postgresql://test_user:test_password@localhost/bp_mcp_test' \
  python -m pytest bp_api/tests/test_mcp_database.py bp_api/tests/test_mcp.py -q
```

测试为每个用例创建独立随机 schema，结束后删除该 schema。SQL 迁移、用户权限、并发幂等、真实回测和临时定价在 PostgreSQL 上运行；测试初始化仅去掉 Timescale 存储扩展调用，不验证 hypertable 运维。CI 使用 PostgreSQL 18 自动运行这些集成测试。

协议测试使用官方 MCP 客户端验证握手、工具发现、结构化错误和跨请求身份隔离。Celery 发布路径采用可控的队列替身验证，生产 Redis/Celery 的连通性需在部署环境确认。
