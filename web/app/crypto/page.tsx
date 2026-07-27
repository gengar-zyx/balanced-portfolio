import { Suspense } from "react";
import type { Metadata } from "next";
import { getCachedCryptoCorrelation } from "@/lib/cached-data";
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
  // SSR 预取: 命中 Next.js "use cache" (cacheTag "crypto") 即时返回; miss 则后端走
  // Redis (crypto:correlation:v{version}) → 预计算表, 请求路径永不计算。
  const data = await getCachedCryptoCorrelation();
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
