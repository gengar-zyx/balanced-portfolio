import { Suspense } from "react";
import type { Metadata } from "next";
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

export default function CryptoPage() {
  return (
    <Suspense
      fallback={
        <div className="min-h-screen bg-background flex items-center justify-center">
          <div className="text-muted-foreground text-sm">加载中...</div>
        </div>
      }
    >
      <CryptoClient />
    </Suspense>
  );
}
