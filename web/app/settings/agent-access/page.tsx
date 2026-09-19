"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/lib/auth";
import { api, type AgentScope } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

const SCOPE_LABELS: Record<AgentScope, string> = {
  read: "查询数据与计算结果",
  "portfolio:write": "创建、修改和复制自己的组合",
  compute: "提交回测与临时定价",
};

export default function AgentAccessPage() {
  const { ready, userId, email, totpEnabled, mustSetup2fa } = useAuth();
  // Remount all sensitive state if the signed-in account changes.
  if (!ready) return <main className="mx-auto max-w-4xl p-6">正在加载账户…</main>;
  if (!email || userId == null) return <main className="mx-auto max-w-4xl p-6"><h1 className="text-2xl font-semibold">Agent 访问</h1><p className="mt-3 text-muted-foreground">请通过右上角登录后管理访问令牌。</p></main>;
  return <AgentAccess key={userId} userId={userId} totpEnabled={totpEnabled} mustSetup2fa={mustSetup2fa} />;
}

function AgentAccess({ userId, totpEnabled, mustSetup2fa }: { userId: number; totpEnabled: boolean; mustSetup2fa: boolean }) {
  const client = useQueryClient();
  const queryKey = ["agent-tokens", userId];
  const { data, isPending, error: loadError } = useQuery({ queryKey, queryFn: api.listAgentTokens, staleTime: 0 });
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<AgentScope[]>(["read"]);
  const [days, setDays] = useState<30 | 90 | 365>(90);
  const [otp, setOtp] = useState("");
  const [secret, setSecret] = useState<{ token: string; token_id: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [endpoint, setEndpoint] = useState("/mcp");
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    setEndpoint(`${window.location.origin}/mcp`);
    return () => { alive.current = false; };
  }, []);

  function toggle(scope: AgentScope, checked: boolean) {
    setScopes((current) => {
      const next = new Set(current);
      if (checked) next.add(scope); else next.delete(scope);
      if (checked && scope === "portfolio:write") next.add("compute");
      if (!checked && scope === "compute") next.delete("portfolio:write");
      return [...next];
    });
  }

  async function create(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setError(""); setMessage(""); setSecret(null);
    try {
      const created = await api.createAgentToken({ name: name.trim(), scopes, expires_days: days, otp_code: totpEnabled ? otp : undefined });
      if (!alive.current) return;
      setSecret(created); setName(""); setOtp("");
      await client.invalidateQueries({ queryKey });
    } catch (e) {
      if (alive.current) setError(e instanceof Error ? e.message : "创建失败，请重试");
    } finally { if (alive.current) setBusy(false); }
  }

  async function revoke(id: string) {
    setBusy(true); setError(""); setMessage("");
    try {
      await api.revokeAgentToken(id);
      if (!alive.current) return;
      if (secret?.token_id === id) setSecret(null);
      setMessage("令牌已撤销，后续请求将立即失效。");
      await client.invalidateQueries({ queryKey });
    } catch (e) { if (alive.current) setError(e instanceof Error ? e.message : "撤销失败"); }
    finally { if (alive.current) setBusy(false); }
  }

  async function copy() {
    if (!secret) return;
    try { await navigator.clipboard.writeText(secret.token); setMessage("已复制令牌。"); }
    catch { setError("无法访问剪贴板，请手动选择并复制令牌。"); }
  }

  return <main className="mx-auto w-full max-w-4xl space-y-6 px-4 py-8 sm:px-6">
    <div><h1 className="text-2xl font-semibold tracking-tight">Agent 访问</h1><p className="mt-2 text-sm text-muted-foreground">让你的 agent 查询数据、管理组合并提交计算。每个令牌独立授权，可随时撤销。</p></div>
    <Card><CardHeader><CardTitle>MCP 连接</CardTitle><CardDescription>在支持 Streamable HTTP 的客户端中填写服务地址，并将令牌配置为 Bearer Token。</CardDescription></CardHeader>
      <CardContent><code className="block break-all rounded-md bg-muted p-3 text-sm">{endpoint}</code><p className="mt-3 text-xs text-muted-foreground">访问范围限于自己的数据和公开示例。暂不提供删除、后台管理和合约写入。</p></CardContent></Card>
    {(error || loadError) && <p role="alert" className="rounded-md border border-destructive/30 p-3 text-sm text-destructive">{error || (loadError instanceof Error && loadError.message !== "Not Found" ? loadError.message : "无法加载令牌；请确认服务已启用 MCP 并完成数据库升级。")}</p>}
    {message && <p role="status" className="text-sm text-muted-foreground">{message}</p>}
    {secret && <Card className="border-primary"><CardHeader><CardTitle>保存你的令牌</CardTitle><CardDescription>明文仅显示这一次，离开或刷新页面后无法再次查看。</CardDescription></CardHeader><CardContent className="space-y-3">
      <code data-testid="agent-secret" className="block select-all break-all rounded-md bg-muted p-3 text-sm">{secret.token}</code>
      <div className="flex gap-2"><Button onClick={copy} variant="secondary">复制令牌</Button><Button variant="outline" onClick={() => setSecret(null)}>已保存，隐藏</Button></div>
    </CardContent></Card>}
    <Card><CardHeader><CardTitle>创建令牌</CardTitle><CardDescription>默认仅允许查询；组合写入同时需要计算权限。</CardDescription></CardHeader><CardContent>
      <form className="space-y-5" onSubmit={create}>
        <div className="grid gap-4 sm:grid-cols-2"><label className="space-y-2 text-sm"><span>令牌名称</span><Input required maxLength={100} placeholder="例如：研究助手" value={name} onChange={(e) => setName(e.target.value)} /></label>
          <label className="space-y-2 text-sm"><span>有效期</span><select className="flex h-9 w-full rounded-md border border-input bg-background px-3 text-sm" value={days} onChange={(e) => setDays(Number(e.target.value) as 30 | 90 | 365)}><option value={30}>30 天</option><option value={90}>90 天</option><option value={365}>365 天</option></select></label></div>
        <fieldset className="space-y-3"><legend className="mb-3 text-sm font-medium">访问权限</legend>{(Object.keys(SCOPE_LABELS) as AgentScope[]).map((scope) => <label key={scope} className="flex items-center gap-3 text-sm"><input type="checkbox" checked={scopes.includes(scope)} onChange={(e) => toggle(scope, e.target.checked)} className="h-4 w-4 accent-current" />{SCOPE_LABELS[scope]}</label>)}</fieldset>
        {totpEnabled && <label className="block max-w-xs space-y-2 text-sm"><span>两步验证码</span><Input required inputMode="numeric" pattern="[0-9]{6}" maxLength={6} autoComplete="one-time-code" value={otp} onChange={(e) => setOtp(e.target.value)} /></label>}
        <Button type="submit" disabled={busy || !!loadError || mustSetup2fa || !name.trim() || !scopes.length}>{busy ? "处理中…" : "创建令牌"}</Button>
      </form>
    </CardContent></Card>
    <Card><CardHeader><CardTitle>已有令牌</CardTitle><CardDescription>这里只显示识别前缀，不保存可再次展示的明文。</CardDescription></CardHeader><CardContent>
      {isPending ? <p className="text-sm text-muted-foreground">正在加载…</p> : !data?.tokens.length ? <p className="text-sm text-muted-foreground">尚未创建令牌。</p> : <ul className="divide-y">{data.tokens.map((token) => {
        const expired = new Date(token.expires_at).getTime() <= Date.now();
        const disabled = !!token.revoked_at || expired;
        return <li key={token.token_id} className="flex flex-col gap-3 py-4 first:pt-0 sm:flex-row sm:items-start sm:justify-between"><div className="min-w-0 space-y-1"><p className="break-words font-medium">{token.name} <span className="text-xs font-normal text-muted-foreground">{token.revoked_at ? "已撤销" : expired ? "已过期" : "有效"}</span></p><p className="font-mono text-xs text-muted-foreground">{token.prefix}…</p><p className="text-xs text-muted-foreground">{token.scopes.map((s) => SCOPE_LABELS[s]).join(" · ")}</p><p className="text-xs text-muted-foreground">到期：{new Date(token.expires_at).toLocaleDateString()} · 最近使用：{token.last_used_at ? new Date(token.last_used_at).toLocaleString() : "尚未使用"}</p></div><Button variant="outline" size="sm" disabled={busy || disabled} onClick={() => revoke(token.token_id)}>撤销<span className="sr-only"> {token.name}</span></Button></li>;
      })}</ul>}
    </CardContent></Card>
  </main>;
}
