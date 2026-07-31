// /crypto 看板与首页 crypto section 共享的格式化与常量, 防两处分叉。
// 逻辑与 web/app/crypto/CryptoClient.tsx 的 fmtPrice/fmtCorr/fmtTz 完全一致。

import type { CryptoMeta } from "./api";

/** 快照中数值价格字段 (排除 as_of)。 */
export type CryptoPriceField =
  | "btc"
  | "dxy"
  | "comex_gold"
  | "au0_gold"
  | "sp500"
  | "nasdaq";

/** $64.1k (>=1000) / $884.20 / -- 。 */
export function fmtPrice(v: number | null): string {
  if (v == null) return "--";
  if (v >= 1000) return `$${(v / 1000).toFixed(1)}k`;
  return `$${v.toFixed(2)}`;
}

/** 按字段格式化: DXY 两位小数, 标普/纳指 整数千分位, 金属/BTC 用 fmtPrice。 */
export function fmtAssetPrice(field: CryptoPriceField, v: number | null): string {
  if (v == null) return "--";
  switch (field) {
    case "dxy":
      return v.toFixed(2);
    case "sp500":
    case "nasdaq":
      return Math.round(v).toLocaleString("en-US");
    default:
      return fmtPrice(v);
  }
}

export function fmtCorr(v: number | null): string {
  if (v == null) return "--";
  return v.toFixed(4);
}

/** 相关性数组末尾首个非 null (最新滚动相关值)。 */
export function lastCorr(arr: (number | null)[] | undefined): number | null {
  if (!arr?.length) return null;
  for (let i = arr.length - 1; i >= 0; i--) {
    const v = arr[i];
    if (v != null && !Number.isNaN(v)) return v;
  }
  return null;
}

function fmtTz(iso: string, tz: string, suffix: string): string {
  try {
    return (
      new Intl.DateTimeFormat("zh-CN", {
        timeZone: tz,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }).format(new Date(iso)) + suffix
    );
  } catch {
    return new Date(iso).toLocaleString("zh-CN", { timeZone: tz }) + suffix;
  }
}

/** 优先用后端预算好的 as_of_et/as_of_cn (确定性); 缺失则 Intl 从 as_of 兜底。 */
export function fmtCryptoAsOf(meta: CryptoMeta | undefined): {
  et: string | null;
  cn: string | null;
} {
  if (!meta) return { et: null, cn: null };
  const et =
    meta.as_of_et ??
    (meta.as_of ? fmtTz(meta.as_of, "America/New_York", " ET") : null);
  const cn =
    meta.as_of_cn ??
    (meta.as_of ? fmtTz(meta.as_of, "Asia/Shanghai", " 北京时间") : null);
  return { et, cn };
}

/** 4 个 corr pair 的展示顺序 (匹配首页 mockup)。headline=COMEX (BTC vs 黄金叙事)。 */
export const CRYPTO_PAIR_ORDER = [
  { key: "au0_gold", label: "沪金AU0", headline: false },
  { key: "comex_gold", label: "COMEX", headline: true },
  { key: "nasdaq", label: "纳斯达克", headline: false },
  { key: "sp500", label: "标普500", headline: false },
] as const;

/** 琥珀「数据待齐」徽章样式 (复用 CryptoClient.tsx 的口径)。 */
export const AMBER_BADGE_CLASS =
  "text-amber-700/80 dark:text-amber-400/80 border-amber-500/30";
