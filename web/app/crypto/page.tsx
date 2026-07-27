import { Suspense } from "react";
import { connection } from "next/server";
import type { Metadata } from "next";
import { getCachedCryptoCorrelation } from "@/lib/cached-data";
import type { CryptoCorrelationResponse } from "@/lib/api";
import { CryptoClient } from "./CryptoClient";

export const metadata: Metadata = {
  title: "加密货币看板 — BTC 相关性分析",
  description:
    "比特币 90 日滚动相关性分析：BTC 与黄金、标普500、纳斯达克100 的相关性走势，以及 BTC 与美元指数 (DXY) 的远期相关性。",
  alternates: { canonical: "/crypto" },
  openGraph: {
    title: "加密货币看板 | Balanced Portfolio",
    description:
      "比特币 90 日滚动相关性分析：BTC 与黄金、标普500、纳斯达克100 的相关性走势，以及 BTC 与美元指数 (DXY) 的远期相关性。",
    url: "/crypto",
  },
};

export default async function CryptoPage() {
  // Cache Components 下用 connection() 退出静态预渲染 (build/CI 时 FastAPI 未运行,
  // 预渲染会 fetch 127.0.0.1:8000 失败致构建中断)。运行时按需 SSR,
  // getCachedCryptoCorrelation 的 "use cache" 仍缓存数据, 失败 throw (不缓存 null, 无 stale null)。
  await connection();
  // SSR 预取: 命中 Next.js "use cache" (cacheTag "crypto") 即时返回; miss 则后端走
  // Redis (crypto:correlation:v{version}) → 预计算表, 请求路径永不计算。
  // 失败时 catch → data=null (CryptoClient 显示「获取数据失败」), 但 getCachedCryptoCorrelation
  // throw 而非返回 null, 故 "use cache" 不缓存 null, 下次请求自动重试 (无 stale null)。
  let data: CryptoCorrelationResponse | null = null;
  try {
    data = await getCachedCryptoCorrelation();
  } catch (e) {
    console.error("crypto SSR fetch failed:", e);
  }
  return (
    <Suspense
      fallback={
        <div className="min-h-screen bg-background flex items-center justify-center">
          <div className="text-muted-foreground text-sm">加载中...</div>
        </div>
      }
    >
      <CryptoClient data={data} />
    </Suspense>
  );
}
