# Balanced Portfolio

[![CI](https://github.com/hxlog/balanced-portfolio/actions/workflows/ci.yml/badge.svg)](https://github.com/hxlog/balanced-portfolio/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

**Balanced Portfolio 是基于桥水基金风险平价理论的投资组合管理与回测系统，包括中金所股指期货描述性统计和场外期权自动敲入敲出产品(autocallables)结构化产品定价功能**，供广大投资者参考和交流学习。

项目仓库：[github.com/hxlog/balanced-portfolio](https://github.com/hxlog/balanced-portfolio)

Demo：https://xushilu.com

Demo测试账户和密码都是`test1`

介绍与说明（长文）：[我全栈开发的第一个产品：Balanced Portfolio 多资产风险平价全天候策略回测系统](https://prologue.dev/blog/my-first-full-stack-development-project-balanced-portfolio-a-global-multi-asset-risk-parity-backtesting-system)

## 主要功能

- **风险平价组合**：按GDP增长/宏观通胀二分法划分四象限，将四种不同经济场景里分配对应经济场景占优的投资品种，优化算法包括最大化夏普/Sortino，支持象限内最大优化指标、象限间等风险贡献，以及全资产风险平价、按优化指标分配风险预算共四种方法。结果包含优化后的投资品权重、净值、调仓、风险指标、相关性和绩效归因。
- **中金所股指期货数据看板**：跟踪 IF、IH、IC、IM 及对应指数，展示同一交易日口径的收盘快照、期限结构、历史年化升贴水和统计分位。
- **加密货币相关性看板**：比特币对数日收益率与黄金（COMEX/沪金）、标普500、纳斯达克100 的滚动相关系数（Pearson/Spearman/Kendall/Hoeffding），以及 BTC 与美元指数 (DXY) 的远期走势。预计算落库、NYSE 16:00 ET 双时区「数据截至」带时分、Next.js→Redis→DB 三层缓存。
- **场外期权自动敲入敲出(autocallables)结构化产品定价**：覆盖雪球、凤凰、气囊和障碍产品，支持Monte Carlo、BSM求解、积分法定价，模型求解输出公允价值、PV值、PoL、Delta/Gamma/Vega/Theta/Rho 与存续路径状态。
- **机构级别的数据可视化**：结合了机构常用的金融工程求解方法，打造一个开箱即用、可交互的、可视化的分析与回测工具。

## 功能展示

### 风险平价回测系统

![风险平价回测结果](/docs/balanced_portfolio_dashboard_example.avif)

### 股指期货数据看板

![股指期货数据看板](/docs/balanced_portfolio_futures.jpeg)

### 场外衍生品定价

![场外衍生品定价](/docs/balanced_portfolio_otc_derivatives_pricing.avif)

## 技术栈

行情通过 [AKShare](https://github.com/akfamily/akshare) 获取并写入 PostgreSQL/TimescaleDB，底层来源包括交易所及东财、新浪等公开接口，具体来源由资产配置决定。原始行情不会直接进入回测：系统先按 A 股交易日历对齐，线性填补内部缺口，截断前导缺口，并对没有右端锚点的尾部缺口报错。回测在每个交易日只使用当日及之前的滚动窗口数据，测试会验证修改未来收益不会改变历史净值。

组合回测、OTC 定价和全量行情任务采用异步执行。生产环境使用 Redis + Celery；本地开发可设置 `BP_TASK_MODE=inline`，由 FastAPI `BackgroundTasks` 执行，不需要启动 Redis 和 worker。

架构和数据流见 [docs/architecture.md](docs/architecture.md)。

## 环境要求

- Python 3.11–3.14
- Node.js 20+
- PostgreSQL 18
- TimescaleDB 2.23+（建议使用当前稳定版）
- `psql` 命令行工具

## Quick Start

1. 克隆仓库并创建配置：

   ```bash
   git clone https://github.com/hxlog/balanced-portfolio.git
   cd balanced-portfolio
   cp .env.example .env
   ```

   编辑 `.env`，至少填写数据库连接、JWT 密钥和首次管理员凭据。开发环境可使用 inline 任务模式：

   ```env
   PGPASSWORD=<database-password>
   BP_JWT_SECRET=<random-long-secret>
   <!-- JWT推荐生成高强度密钥 -->
   BP_ADMIN_EMAIL=admin@example.com
   <!-- 管理员账户的又想和密码 -->
   BP_ADMIN_INITIAL_PASSWORD=<initial-admin-password>
   <!-- inline是开发模式，生产环境可使用redis（填写celery） -->
   BP_TASK_MODE=inline
   ```

   可用 `python -c "import secrets; print(secrets.token_urlsafe(48))"` 生成 JWT 密钥。管理员创建成功后可从运行环境移除 `BP_ADMIN_INITIAL_PASSWORD`。

2. 安装 Python 依赖：

   ```bash
   python -m venv .venv
   # Linux/macOS
   source .venv/bin/activate
   # Windows PowerShell
   # .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

3. 初始化数据库。全新环境只执行合并后的 schema，不要再逐个执行旧编号脚本：

   ```bash
   createdb -h localhost -U postgres balanced_portfolio
   psql -h localhost -U postgres -d balanced_portfolio -f ddl/schema.sql
   ```

   数据库已存在时跳过 `createdb`。

4. 拉取并清洗行情：

   ```bash
   python -m bp_ingest run
   ```

5. 启动后端：

   ```bash
   uvicorn bp_api.main:app --host 127.0.0.1 --port 8000 --reload
   ```

6. 在另一个终端启动前端：

   ```bash
   cd web
   npm install --legacy-peer-deps
   npm run dev
   ```

打开 <http://localhost:3000>。后端健康检查地址为 <http://localhost:8000/api/health>，OpenAPI 页面为 <http://localhost:8000/docs>。若 API 不在本机 `8000` 端口，启动前端前设置 `BP_API_BASE`。

## 常用命令

```bash
# 行情调度测试
python -m bp_ingest ping

# 行情调度启动
python -m bp_ingest run

# 行情调度只提取沪深300和恒生指数，不自动清洗数据
python -m bp_ingest run --symbols 000300 HSI --no-clean

# 清洗行情数据，将交易日标准化成A股交易日，再计算涨跌幅
python -m bp_ingest clean

# 行情调度任务，建议配合pm2使用
python -m bp_ingest schedule

# 中金所股指期货行情跑增量
python -m bp_ingest cffex-backfill

# 中金所股指期货行情跑全量（会删除历史期货行情数据！！！）
python -m bp_ingest cffex-backfill --full 

# 中金所股指期货数据计算基差和贴水
python -m bp_ingest cffex-backfill --recompute-premium

# 后端服务启动，建议配合pm2
uvicorn bp_api.main:app --host 127.0.0.1 --port 8000 --reload

# redis celery查询
celery -A bp_api.workers.celery_app worker -c 2

# 前端next.js的开发与build命令
cd web
npm run dev
npm run build
```

## 测试

```bash
# CI测试
python -m pytest bp_api/tests -q
cd web && npm run build
```

后端测试覆盖优化权重、ERC、绩效指标、CFFEX 数据口径、OTC 定价和无未来函数约束。提交前请同时确认前端生产构建通过。

## 部署与协作

- 部署说明：[docs/deployment.md](docs/deployment.md)
- 生产运维速查：[deploy/OPS.md](deploy/OPS.md)
- 问题反馈：[GitHub Issues](https://github.com/hxlog/balanced-portfolio/issues)

## 许可证

本项目按 [Apache License 2.0](LICENSE) 发布。第三方组件仍适用各自许可证。

## 更新日志（自 v1.0.0）

### 新功能

- **加密货币相关性看板**（`/crypto`）：BTC 与黄金（COMEX/沪金）、标普500、纳斯达克100 的对数日收益率滚动相关性 + BTC vs 美元指数 (DXY) 远期走势。4 种方法（Pearson/Spearman/Kendall/Hoeffding）×4 窗口（3/6/9/12 月）。预计算落库（`bp_crypto_corr_daily` 64 series-per-row JSONB + `bp_crypto_meta`），NYSE 16:00 ET 双时区「数据截至」带时分（ET + 北京时间，DST 自洽），`bp_ingest.run` 钩子 + 每日定时任务重算，API 只读、请求路径永不计算，Next.js→Redis→DB 三层缓存。
- **yfinance 数据源**：crypto/forex/commodity 日线（BTC-USD、美元指数 DXY、COMEX 黄金期货），与 AKShare 行情共用 `bp_ingest` 增量/清洗/调度链路。

### 改进

- 回测引擎：归因（attribution）、绩效指标（metrics）、backtest 无未来函数约束（前缀和滚动增量矩，O(n²)）优化。
- 前端：Navbar/_dashboard/api 客户端改进；资产清单 SSR 缓存 revalidate（`cacheTag` + `/api/revalidate`）。
- CFFEX 看板：全量回填脚本（按月 ZIP 并发）+ 收盘确认 fetch 修复。

### 修复

- 修复前端资产缓存未 revalidate 的问题（`ceb3499`）。
- 修复 CFFEX 期货补全量脚本（`f83ddeb`）。
- 修复未收盘就 fetch 指数 close 价格的 bug（`ea3b340`，盘中脏价污染正式收盘）。

### 维护

- 删除 `scripts/recompute.py`；README 文字/demo 账户更新；`ddl/schema.sql` 合并 `33_crypto_corr`（crypto 预计算表 + 孤儿表清理）。

## 数据与投资免责声明

AKShare 聚合公开数据接口，数据源可能调整、延迟、限流、修订或停止服务。项目不保证行情、交易日历、复权、基差、波动率、定价结果和回测结果完整、准确或持续可用，使用者应自行核验数据授权、质量和适用性。

本项目仅用于软件开发、教学和研究，不构成投资建议、交易信号、估值意见、要约或任何收益承诺。回测和模型价格不代表未来表现或可成交价格，实盘决策及损失由使用者自行承担。
