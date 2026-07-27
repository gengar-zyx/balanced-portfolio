import { NextRequest, NextResponse } from "next/server";
import { revalidateTag } from "next/cache";

/**
 * 失效 /crypto 的 SSR 缓存 (cacheTag "crypto", cacheLife "hours")。
 *
 * 由后端 ingest / 调度任务在重算 crypto 相关性后用内部令牌调用 (X-Internal-Token),
 * 让 /crypto 下次访问立即看到新数据, 不必等 cacheLife("hours") TTL。
 *
 * 路由由 Next Route Handler 处理 (文件系统路由优先, 不被 /api/:path* rewrite 转给 FastAPI),
 * 与 web/app/api/revalidate/route.ts (admin Bearer, 失效 assets tag) 解耦。
 * 令牌: BP_INTERNAL_REVALIDATE_TOKEN (后端 ingest worker 与 Next.js 同设, 32+ 字符随机)。
 */
export async function POST(request: NextRequest) {
  const expected = process.env.BP_INTERNAL_REVALIDATE_TOKEN;
  if (!expected) {
    return NextResponse.json(
      { detail: "未配置内部令牌 BP_INTERNAL_REVALIDATE_TOKEN" },
      { status: 503 },
    );
  }
  const token = request.headers.get("x-internal-token");
  if (token !== expected) {
    return NextResponse.json({ detail: "令牌无效" }, { status: 401 });
  }
  // 第二参数 "hours" 须与 getCachedCryptoCorrelation 的 cacheLife("hours") 一致, 否则失效不到该缓存。
  revalidateTag("crypto", "hours");
  return NextResponse.json({ ok: true, tag: "crypto" });
}
