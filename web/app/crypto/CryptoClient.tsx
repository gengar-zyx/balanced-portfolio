"use client";

import { useState, useEffect, useMemo } from "react";
import { Activity, TrendingUp } from "lucide-react";
import { useTheme } from "next-themes";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { EChart } from "@/components/EChart";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface PairData {
  label: string;
  dates: string[];
  correlation: (number | null)[];
  btc_price: (number | null)[];
}

interface ShiftedPrice {
  dates: string[];
  btc: (number | null)[];
  dxy: (number | null)[];
}

interface Snapshot {
  btc: number | null;
  dxy: number | null;
  comex_gold: number | null;
  au0_gold: number | null;
  as_of: string;
}

interface CorrelationResponse {
  snapshot: Snapshot;
  rolling: Record<string, Record<string, Record<string, PairData>>>;
  lagged_shifted: Record<string, ShiftedPrice>;
  meta: {
    computed_at: string;
    btc_data_end: string | null;
    window_sizes: Record<string, number>;
    methods: string[];
    assets: Record<string, string>;
    calendar: string;
  };
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const WINDOWS = [
  { value: "3M", label: "3 个月" },
  { value: "6M", label: "6 个月" },
  { value: "9M", label: "9 个月" },
  { value: "12M", label: "12 个月" },
] as const;

const METHODS = [
  { value: "pearson", label: "Pearson" },
  { value: "spearman", label: "Spearman" },
  { value: "kendall", label: "Kendall τ" },
  { value: "hoeffding", label: "Hoeffding D" },
] as const;

const LAG_OPTIONS = [
  { value: "3M", label: "3 个月" },
  { value: "6M", label: "6 个月" },
  { value: "9M", label: "9 个月" },
  { value: "12M", label: "12 个月" },
] as const;

const CORR_COLORS_LIGHT: Record<string, string> = {
  comex_gold: "#D97706",
  au0_gold: "#F59E0B",
  sp500: "#3B82F6",
  nasdaq: "#10B981",
};

const CORR_COLORS_DARK: Record<string, string> = {
  comex_gold: "#F59E0B",
  au0_gold: "#FBBF24",
  sp500: "#6C8EEF",
  nasdaq: "#34D399",
};

const CHART1_DEFAULT_ACTIVE = new Set(["COMEX黄金", "纳斯达克100", "BTC 价格 (USD)"]);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function fetchJSON<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `${res.status}`);
  }
  return res.json();
}

function fmtPrice(v: number | null): string {
  if (v == null) return "--";
  if (v >= 1000) return `$${(v / 1000).toFixed(1)}k`;
  return `$${v.toFixed(2)}`;
}

function fmtCorr(v: number | null): string {
  if (v == null) return "--";
  return v.toFixed(4);
}

// ---------------------------------------------------------------------------
// CryptoClient
// ---------------------------------------------------------------------------

export function CryptoClient() {
  const { resolvedTheme } = useTheme();
  const isDark = resolvedTheme === "dark";

  const [data, setData] = useState<CorrelationResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [windowMonths, setWindowMonths] = useState("3M");
  const [corrMethod, setCorrMethod] = useState("pearson");
  const [lagHorizon, setLagHorizon] = useState("3M");

  useEffect(() => {
    fetchJSON<CorrelationResponse>("/api/crypto/correlation")
      .then(setData)
      .catch((e) => setError(e instanceof Error ? e.message : "获取数据失败"));
  }, []);

  // --- Theme colors ---
  const cardBg = isDark ? "rgba(22,22,22,0.9)" : "#fff";
  const fg = isDark ? "#EDEDED" : "#171717";
  const textCol = isDark ? "#A1A1A1" : "#666";
  const axisLineCol = isDark ? "#333" : "#ddd";
  const splitLineCol = isDark ? "rgba(255,255,255,0.06)" : "rgba(0,0,0,0.06)";
  const corrColors = isDark ? CORR_COLORS_DARK : CORR_COLORS_LIGHT;

  const currentRolling = useMemo(() => {
    if (!data) return null;
    return data.rolling[windowMonths]?.[corrMethod] ?? null;
  }, [data, windowMonths, corrMethod]);

  const shiftedPrices = useMemo(() => {
    return data?.lagged_shifted?.[lagHorizon] ?? null;
  }, [data, lagHorizon]);

  const currentRSummary = useMemo(() => {
    if (!currentRolling) return {};
    const result: Record<string, number | null> = {};
    for (const [key, pair] of Object.entries(currentRolling)) {
      const valid = (pair.correlation ?? []).filter((v) => v != null);
      result[key] = valid.length > 0 ? valid[valid.length - 1] : null;
    }
    return result;
  }, [currentRolling]);

  // --- Chart 1: 滚动相关性 + BTC 价格 (双 Y 轴, NYSE 日历) ---
  const chart1Option = useMemo(() => {
    if (!currentRolling) return {};
    const pairs = Object.entries(currentRolling);
    if (!pairs.length) return {};

    const firstPair = pairs[0][1];
    if (!firstPair.dates.length) return {};
    const dates = firstPair.dates;

    const series: any[] = [];
    const legendData: string[] = [];
    const legendSelected: Record<string, boolean> = {};
    const btcAreaColor = isDark ? "#6C8EEF" : "#3B82F6";

    // 相关性线 (左轴)
    for (const [key, pair] of pairs) {
      if (!pair.dates.length) continue;
      const color = corrColors[key] ?? (isDark ? "#888" : "#666");
      // dates 已全部对齐到 NYSE 日历, 直接用
      series.push({
        name: pair.label,
        type: "line", smooth: true, yAxisIndex: 0, symbol: "none",
        lineStyle: { width: 2, color },
        itemStyle: { color },
        data: pair.correlation,
      });
      legendData.push(pair.label);
      legendSelected[pair.label] = CHART1_DEFAULT_ACTIVE.has(pair.label);
    }

    // BTC 价格 (右轴, 面积图)
    const btcPrices = firstPair.btc_price;
    const btcName = "BTC 价格 (USD)";
    if (btcPrices?.some((p) => p != null)) {
      series.push({
        name: btcName, type: "line", smooth: true, yAxisIndex: 1, symbol: "none",
        lineStyle: { width: 1.5, color: btcAreaColor },
        itemStyle: { color: btcAreaColor },
        areaStyle: {
          color: {
            type: "linear", x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: isDark ? "rgba(108,142,239,0.15)" : "rgba(59,130,246,0.12)" },
              { offset: 1, color: "rgba(255,255,255,0)" },
            ],
          },
        },
        data: btcPrices,
      });
      legendData.push(btcName);
      legendSelected[btcName] = true;
    }

    return {
      backgroundColor: "transparent",
      tooltip: {
        trigger: "axis", backgroundColor: cardBg,
        borderColor: isDark ? "#333" : "#e5e5e5",
        textStyle: { color: fg, fontSize: 12 },
        formatter: (params: any) => {
          if (!Array.isArray(params)) return "";
          let html = `<div style="font-size:11px;margin-bottom:4px">${params[0].axisValue}</div>`;
          for (const p of params) {
            const val = p.value;
            if (val == null) continue;
            const marker = `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${p.color};margin-right:4px"></span>`;
            if (p.seriesName.includes("BTC")) {
              html += `<div style="margin:2px 0">${marker}${p.seriesName}: <b>$${Number(val).toLocaleString()}</b></div>`;
            } else {
              html += `<div style="margin:2px 0">${marker}${p.seriesName}: <b>${Number(val).toFixed(4)}</b></div>`;
            }
          }
          return html;
        },
      },
      legend: {
        type: "scroll", bottom: 30,
        textStyle: { color: textCol, fontSize: 11 },
        data: legendData, selected: legendSelected,
      },
      dataZoom: [
        { type: "slider", bottom: 0, start: 55, end: 100, height: 22, textStyle: { color: textCol, fontSize: 10 } },
        { type: "inside" },
      ],
      grid: { left: 65, right: 75, top: 15, bottom: 75 },
      xAxis: {
        type: "category", boundaryGap: false, data: dates,
        axisLine: { lineStyle: { color: axisLineCol } },
        axisLabel: { color: textCol, fontSize: 10 },
      },
      yAxis: [
        {
          type: "value", name: "滚动相关系数", min: -1, max: 1,
          nameTextStyle: { color: textCol, fontSize: 11 },
          axisLabel: { color: textCol, fontSize: 10, formatter: (v: number) => v.toFixed(1) },
          splitLine: { lineStyle: { color: splitLineCol, type: "dashed" } },
        },
        {
          type: "value", name: "BTC 价格 (USD)",
          nameTextStyle: { color: textCol, fontSize: 11 },
          axisLabel: { color: textCol, fontSize: 10, formatter: (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(0)}k` : v.toFixed(0)) },
          splitLine: { show: false },
        },
      ],
      series,
    };
  }, [currentRolling, isDark, cardBg, fg, textCol, axisLineCol, splitLineCol, corrColors]);

  // --- Chart 2: BTC vs DXY 滞后平移价格 (双 Y 轴, 自适应刻度) ---
  const chart2Option = useMemo(() => {
    if (!shiftedPrices || !shiftedPrices.dates.length) return {};

    const btcColor = isDark ? "#6C8EEF" : "#3B82F6";
    const dxyColor = isDark ? "#F59E0B" : "#D97706";

    return {
      backgroundColor: "transparent",
      tooltip: {
        trigger: "axis", backgroundColor: cardBg,
        borderColor: isDark ? "#333" : "#e5e5e5",
        textStyle: { color: fg, fontSize: 12 },
        formatter: (params: any) => {
          if (!Array.isArray(params)) return "";
          const date = params[0]?.axisValue ?? "";
          let html = `<div style="font-size:11px;margin-bottom:4px">${date}</div>`;
          for (const p of params) {
            const val = p.value;
            if (val == null) continue;
            const marker = `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${p.color};margin-right:4px"></span>`;
            const formatted = p.seriesName.includes("BTC") ? `$${Number(val).toLocaleString()}` : Number(val).toFixed(2);
            html += `<div style="margin:2px 0">${marker}${p.seriesName}: <b>${formatted}</b></div>`;
          }
          return html;
        },
      },
      legend: {
        bottom: 30,
        textStyle: { color: textCol, fontSize: 11 },
        data: ["BTC 价格 (USD)", `DXY (+${lagHorizon})`],
      },
      dataZoom: [
        { type: "slider", bottom: 0, start: 0, end: 100, height: 22, textStyle: { color: textCol, fontSize: 10 } },
        { type: "inside" },
      ],
      grid: { left: 70, right: 70, top: 15, bottom: 75 },
      xAxis: {
        type: "category", boundaryGap: false, data: shiftedPrices.dates,
        axisLine: { lineStyle: { color: axisLineCol } },
        axisLabel: { color: textCol, fontSize: 10 },
      },
      yAxis: [
        {
          type: "value", name: "BTC (USD)", scale: true,
          nameTextStyle: { color: btcColor, fontSize: 11 },
          axisLabel: { color: textCol, fontSize: 10, formatter: (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(0)}k` : v.toFixed(0)) },
          splitLine: { lineStyle: { color: splitLineCol, type: "dashed" } },
        },
        {
          type: "value", name: "DXY", scale: true,
          nameTextStyle: { color: dxyColor, fontSize: 11 },
          axisLabel: { color: textCol, fontSize: 10, formatter: (v: number) => v.toFixed(1) },
          splitLine: { show: false },
        },
      ],
      series: [
        {
          name: "BTC 价格 (USD)", type: "line", smooth: true, yAxisIndex: 0, symbol: "none",
          lineStyle: { width: 2, color: btcColor },
          itemStyle: { color: btcColor },
          areaStyle: {
            color: {
              type: "linear", x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color: isDark ? "rgba(108,142,239,0.15)" : "rgba(59,130,246,0.12)" },
                { offset: 1, color: "rgba(255,255,255,0)" },
              ],
            },
          },
          data: shiftedPrices.btc,
        },
        {
          name: `DXY (+${lagHorizon})`, type: "line", smooth: true, yAxisIndex: 1, symbol: "none",
          lineStyle: { width: 2, color: dxyColor },
          itemStyle: { color: dxyColor },
          data: shiftedPrices.dxy,
        },
      ],
    };
  }, [shiftedPrices, lagHorizon, isDark, cardBg, fg, textCol, axisLineCol, splitLineCol]);

  // --- Render helpers ---
  const snap = data?.snapshot;
  const meta = data?.meta;
  const asOf = snap?.as_of
    ? new Date(snap.as_of).toLocaleDateString("zh-CN", { timeZone: "Asia/Shanghai" })
    : null;
  const methodLabel = METHODS.find((m) => m.value === corrMethod)?.label ?? corrMethod;
  const windowLabel = WINDOWS.find((w) => w.value === windowMonths)?.label ?? windowMonths;

  return (
    <div className="min-h-screen bg-background text-foreground pb-24">
      {/* Page Header */}
      <div className="border-b border-border/40 bg-card/30 backdrop-blur-sm">
        <div className="container mx-auto max-w-7xl px-4 sm:px-6 py-6 sm:py-8">
          <div className="flex flex-col md:flex-row md:items-end justify-between gap-4">
            <div>
              <h1 className="text-2xl sm:text-3xl font-bold tracking-tight mb-2">加密货币看板</h1>
              <p className="text-muted-foreground text-sm">
                比特币相关性分析 — 对数日收益率滚动相关系数与美元指数远期相关性
              </p>
            </div>
            <div className="flex items-center gap-2">
              {asOf && <Badge variant="outline" className="text-muted-foreground text-xs">数据截至: {asOf}</Badge>}
            </div>
          </div>
          <div className="flex flex-col sm:flex-row gap-4 mt-4">
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground whitespace-nowrap">滚动窗口:</span>
              <Tabs value={windowMonths} onValueChange={setWindowMonths}>
                <TabsList className="h-8">
                  {WINDOWS.map((w) => (<TabsTrigger key={w.value} value={w.value} className="text-xs px-3 h-7">{w.label}</TabsTrigger>))}
                </TabsList>
              </Tabs>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground whitespace-nowrap">相关方法:</span>
              <Tabs value={corrMethod} onValueChange={setCorrMethod}>
                <TabsList className="h-8">
                  {METHODS.map((m) => (<TabsTrigger key={m.value} value={m.value} className="text-xs px-3 h-7">{m.label}</TabsTrigger>))}
                </TabsList>
              </Tabs>
            </div>
          </div>
        </div>
      </div>

      <div className="container mx-auto max-w-7xl px-4 sm:px-6 py-6 sm:py-8 space-y-8">
        {error && (
          <div className="bg-destructive/10 border border-destructive/20 rounded-lg px-4 py-3 text-sm text-destructive">
            数据加载异常: {error}
          </div>
        )}

        {/* Snapshot Cards */}
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
          {[
            { label: "BTC/USD", value: fmtPrice(snap?.btc ?? null) },
            { label: "DXY 美元指数", value: snap?.dxy?.toFixed(2) ?? "--" },
            { label: "COMEX 黄金", value: fmtPrice(snap?.comex_gold ?? null) },
            { label: "沪金 AU0", value: fmtPrice(snap?.au0_gold ?? null) },
            { label: `${windowLabel} × ${methodLabel}`, value: "" },
          ].map((item) => (
            <Card key={item.label} className="bg-card/50 shadow-none border-border/50">
              <CardContent className="p-4">
                <div className="text-xs text-muted-foreground mb-1">{item.label}</div>
                <div className="text-xl font-bold tabular-nums">
                  {item.value || <span className="text-xs text-muted-foreground font-normal">{meta?.window_sizes?.[windowMonths]} 日窗口 · NYSE</span>}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>

        {/* Correlation Summary Cards */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {currentRolling && Object.entries(currentRolling).map(([key, pair]) => (
            <Card key={key} className="bg-card/50 shadow-none border-border/50 transition-all hover:bg-card/80">
              <CardContent className="p-4">
                <div className="text-xs text-muted-foreground mb-1">BTC vs {pair.label}</div>
                <div className="text-2xl font-bold tabular-nums" style={{ color: corrColors[key] ?? fg }}>
                  {fmtCorr(currentRSummary[key] ?? null)}
                </div>
                <div className="text-xs text-muted-foreground mt-1">{windowLabel} {methodLabel} R</div>
              </CardContent>
            </Card>
          ))}
        </div>

        {/* Chart 1: 滚动相关性 */}
        <Card className="border-border/50 shadow-sm bg-card/30 backdrop-blur-sm">
          <CardHeader className="border-b border-border/40 pb-4">
            <div className="flex items-center gap-2">
              <TrendingUp className="w-5 h-5 text-muted-foreground" />
              <CardTitle className="text-lg">BTC 滚动相关性 — {windowLabel}窗口 · {methodLabel}</CardTitle>
            </div>
            <p className="text-xs text-muted-foreground mt-1">
              基于 NYSE 交易日历（标普500 交易日），比特币对数日收益率与各资产对数日收益率的 {windowLabel} 滚动相关系数。
              右轴为 BTC 价格 (USD)。数据截止: {meta?.btc_data_end ?? "--"}
            </p>
          </CardHeader>
          <CardContent className="pt-4 pb-2">
            {data ? (
              <EChart option={chart1Option} style={{ height: 440, width: "100%" }} />
            ) : (
              <div className="flex items-center justify-center h-[440px] text-muted-foreground text-sm">
                {error ? "数据加载失败" : "数据加载中..."}
              </div>
            )}
          </CardContent>
        </Card>

        {/* Chart 2: BTC vs DXY 滞后平移价格 */}
        <Card className="border-border/50 shadow-sm bg-card/30 backdrop-blur-sm">
          <CardHeader className="border-b border-border/40 pb-4">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                <Activity className="w-5 h-5 text-muted-foreground" />
                <CardTitle className="text-lg">BTC vs DXY 远期走势</CardTitle>
              </div>
              <Tabs value={lagHorizon} onValueChange={setLagHorizon}>
                <TabsList className="grid grid-cols-4 w-full sm:w-auto h-8">
                  {LAG_OPTIONS.map((lp) => (
                    <TabsTrigger key={lp.value} value={lp.value} className="text-xs px-3 h-7">{lp.label}</TabsTrigger>
                  ))}
                </TabsList>
              </Tabs>
            </div>
            <div className="mt-3 p-3 rounded-lg bg-muted/30 border border-border/30 text-sm text-muted-foreground leading-relaxed">
              美元指数和加密货币市场呈反向关系，这意味着当美元指数上涨时，加密货币价格往往会下跌，反之亦然。
              这是因为加密货币通常被用作对冲美元的工具，当美元强势时，投资者往往会将资金从加密货币转移到美元或其他传统避险资产上。
            </div>
            <p className="text-xs text-muted-foreground mt-2">
              图表展示 BTC 价格 (左轴, 蓝色面积图) 与 DXY 美元指数向后平移 {lagHorizon} 的价格 (右轴, 金色实线)。
              通过平移 DXY 可直观观察两者的领先-滞后关系。
            </p>
          </CardHeader>
          <CardContent className="pt-4 pb-2">
            {data ? (
              <EChart option={chart2Option} style={{ height: 440, width: "100%" }} />
            ) : (
              <div className="flex items-center justify-center h-[440px] text-muted-foreground text-sm">
                {error ? "数据加载失败" : "数据加载中..."}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
