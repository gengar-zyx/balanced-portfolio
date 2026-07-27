import { cacheLife, cacheTag } from "next/cache";
import type { Asset, BacktestResult, CryptoCorrelationResponse } from "./api";
import { apiBase } from "./session-server";

async function serverFetch<T>(path: string): Promise<T> {
  const res = await fetch(`${apiBase()}${path}`);
  if (!res.ok) {
    throw new Error(`API ${path}: ${res.status}`);
  }
  return res.json() as Promise<T>;
}

async function safeServerFetch<T>(path: string): Promise<T | null> {
  try {
    return await serverFetch<T>(path);
  } catch {
    return null;
  }
}

/** 可选资产清单 — 供 Builder 预渲染；变更频率低。 */
export async function getCachedAssets(): Promise<Asset[]> {
  "use cache";
  cacheLife("hours");
  cacheTag("assets");
  const data = await safeServerFetch<{ assets: Asset[] }>("/api/assets");
  return data?.assets ?? [];
}

/** 示例组合回测结果 — 供 Dashboard 访客首屏；回测完成后需等 TTL 或手动 revalidateTag。 */
export async function getCachedDemoResult(
  portfolioId?: number | null,
  method?: string | null,
  benchmark?: string | null,
): Promise<BacktestResult | null> {
  "use cache";
  cacheLife("minutes");
  cacheTag("demo-result");
  const params = new URLSearchParams();
  if (portfolioId != null) params.set("portfolio_id", String(portfolioId));
  if (method) params.set("method", method);
  if (benchmark) params.set("benchmark", benchmark);
  const qs = params.toString();
  try {
    return await serverFetch<BacktestResult>(
      `/api/portfolios/demo${qs ? `?${qs}` : ""}`,
    );
  } catch {
    return null;
  }
}

/** /crypto 相关性看板 — 供 /crypto 首屏 SSR。
 *
 * 后端读预计算表 (bp_crypto_corr_daily / bp_crypto_price_daily / bp_crypto_meta),
 * 请求路径永不计算 (原 27s build_all_correlations 已移到 bp_ingest 调度任务)。
 * 失效: 后端 ingest/调度任务重算后 POST /api/revalidate/crypto (内部令牌) 失效该 tag,
 * 或等 cacheLife("hours") TTL 兜底。 */
export async function getCachedCryptoCorrelation(): Promise<CryptoCorrelationResponse | null> {
  "use cache";
  cacheLife("hours");
  cacheTag("crypto");
  try {
    return await serverFetch<CryptoCorrelationResponse>("/api/crypto/correlation");
  } catch {
    return null;
  }
}