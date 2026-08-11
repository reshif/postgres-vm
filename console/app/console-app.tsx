"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type ColumnDef, flexRender, getCoreRowModel, useReactTable } from "@tanstack/react-table";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  Activity, AlertTriangle, Archive, ArrowDown, ArrowUp, ArrowUpRight, BookOpen, Bug, Check, ChevronDown,
  CircleAlert, Clock3, Database, FileText, GitBranch, GitCompareArrows, ListChecks,
  LogOut, Network, Pin, PinOff, RefreshCw, RotateCcw, Search, Settings2, ShieldCheck, ShieldEllipsis, SlidersHorizontal,
  Sparkles, Users, X
} from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { read, remove, write } from "./api";
import { type Scope, useAsOf, useScope } from "./providers";

type View = "overview" | "inbox" | "knowledge" | "procedures" | "graph" | "timeline" | "conflicts" | "debug" | "health" | "evals" | "settings" | "audit" | "admin";
type Trust = "authoritative" | "verified" | "observed" | "inferred" | "untrusted";
type ExplorerMemory = {
  id: string; title: string; digest: string; type: string; tier: Trust; status: string;
  scope_kind: string; source_type: string; source_uri: string | null; source_version: string | null;
  valid_from: string; valid_until: string | null; recorded_at: string; last_accessed_at: string | null;
  retrieval_count: number; token_cost: number; pinned: boolean; active_at_as_of: boolean;
};
type GraphNode = { id: string; canonical_name: string; kind: string; aliases?: string[]; tier?: Trust; memory_count?: number; relationship_count?: number };
type GraphEdge = { id: string; source_id: string; target_id: string; relation: string; tier: Trust; confidence: number; evidence_memory_id: string; valid_from: string; valid_until: string | null; proposed: boolean };
type Dashboard = {
  window_days: number;
  summary: { requests: number; questions: number };
  outcomes: Array<{ status: string; count: number }>;
  trend: Array<{ date: string; requests: number; questions: number }>;
  top_questions: Array<{ query_text: string; requests: number; last_asked_at: string; answerability: string }>;
  top_knowledge: Array<{ id: string; title: string; type: string; tier: Trust; requests: number; last_used_at: string }>;
};
type ErrorBoxProps = { error: unknown };

type NavGroup = "Knowledge" | "Operations" | "Governance";
const nav: Array<{ href: string; view: View; label: string; icon: typeof BookOpen; group: NavGroup }> = [
  { href: "/inbox/", view: "inbox", label: "Inbox", icon: ShieldCheck, group: "Knowledge" },
  { href: "/knowledge/", view: "knowledge", label: "Explorer", icon: BookOpen, group: "Knowledge" },
  { href: "/procedures/", view: "procedures", label: "Procedures", icon: ListChecks, group: "Knowledge" },
  { href: "/graph/", view: "graph", label: "Graph", icon: Network, group: "Knowledge" },
  { href: "/timeline/", view: "timeline", label: "Timeline", icon: Clock3, group: "Knowledge" },
  { href: "/conflicts/", view: "conflicts", label: "Conflicts", icon: GitCompareArrows, group: "Operations" },
  { href: "/debug/", view: "debug", label: "Debugger", icon: Bug, group: "Operations" },
  { href: "/health/", view: "health", label: "Health", icon: Activity, group: "Operations" },
  { href: "/evals/", view: "evals", label: "Evals", icon: Sparkles, group: "Operations" },
  { href: "/settings/", view: "settings", label: "Settings", icon: Settings2, group: "Governance" },
  { href: "/audit/", view: "audit", label: "Audit", icon: ShieldEllipsis, group: "Governance" },
  { href: "/admin/", view: "admin", label: "Admin", icon: Users, group: "Governance" }
];

const viewMeta: Record<View, { section: string; description: string }> = {
  overview: { section: "Project intelligence", description: "Demand, evidence coverage, and review work for this scoped project." },
  inbox: { section: "Curation", description: "Review proposed knowledge before it can influence retrieval." },
  knowledge: { section: "Project memory", description: "Search, verify, and maintain the evidence available to agents." },
  procedures: { section: "Project memory", description: "Reviewed runbooks and their observed use in this project." },
  graph: { section: "Project memory", description: "Bounded relationships, always tied to recorded evidence." },
  timeline: { section: "Project memory", description: "Inspect valid time separately from the time knowledge was recorded." },
  conflicts: { section: "Curation", description: "Resolve competing claims with a durable audit decision." },
  debug: { section: "Retrieval operations", description: "Replay what the retrieval system examined, selected, and excluded." },
  health: { section: "Retrieval operations", description: "Know whether this project has enough healthy, governed knowledge." },
  evals: { section: "Retrieval operations", description: "Track comparable retrieval-quality evaluations over time." },
  settings: { section: "Governance", description: "Inspect policy-bearing project configuration and capacity signals." },
  audit: { section: "Governance", description: "Review the immutable project-scoped operational trail." },
  admin: { section: "Governance", description: "See the projects available to the current principal." }
};

const trustClass = (tier?: string | null) => `trust trust-${tier || "observed"}`;
const tierClass = (tier?: string | null) => `tier-${tier || "observed"}`;
const formatDate = (value?: string | null) => value ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : "-";
const short = (value?: string | null, length = 10) => value ? value.slice(0, length) : "-";
const asInput = (value: string | null) => value ? new Date(value).toISOString().slice(0, 16) : "";
const answerabilityLabel = (status?: string) => ({
  supported: "Evidence found",
  partial_support: "Partially supported",
  no_relevant_evidence: "No evidence",
  evidence_not_included: "Evidence omitted",
  not_classified: "Earlier request"
}[status || ""] || status || "Not classified");

function ErrorBox({ error }: ErrorBoxProps) {
  return <div className="notice error"><CircleAlert size={16} />{error instanceof Error ? error.message : "Request failed"}</div>;
}

function Loading({ label = "Loading" }: { label?: string }) {
  return <div className="loading" aria-live="polite">{label}</div>;
}

function TrustMark({ tier }: { tier?: string | null }) {
  return <span className={trustClass(tier)} title={`${tier || "observed"} trust`} aria-label={`${tier || "observed"} trust`} />;
}

type OidcConfig = {
  configured: boolean; detail?: string; client_id?: string; scopes?: string;
  redirect_uri?: string | null; resource?: string | null;
  authorization_endpoint?: string; token_endpoint?: string;
};
type ConsoleConfig = Scope & { oauth: boolean; oidc?: OidcConfig; authentication_error?: string };
const OAUTH_TOKEN_KEY = "memory.console.access_token";
const OAUTH_VERIFIER_KEY = "memory.console.pkce_verifier";
const OAUTH_STATE_KEY = "memory.console.pkce_state";

function persistDevScope(scope: Scope) {
  const { access_token: _token, ...saved } = scope;
  window.localStorage.setItem("scope", JSON.stringify(saved));
}

function base64Url(bytes: Uint8Array) {
  let text = "";
  bytes.forEach(value => { text += String.fromCharCode(value); });
  return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

async function pkceChallenge(verifier: string) {
  return base64Url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))));
}

function ScopeBootstrap({ children }: { children: React.ReactNode }) {
  const { scope, setScope } = useScope();
  const [error, setError] = useState<string | null>(null);
  const [configured, setConfigured] = useState<ConsoleConfig | null>(null);
  useEffect(() => {
    let live = true;
    const loadConfig = async (token?: string | null): Promise<ConsoleConfig> => {
      const headers = new Headers();
      if (token) headers.set("Authorization", `Bearer ${token}`);
      const response = await fetch("/v1/console/config", {
        credentials: "same-origin",
        headers,
      });
      if (!response.ok) throw new Error(`Console bootstrap failed (${response.status})`);
      return response.json() as Promise<ConsoleConfig>;
    };
    const load = async () => {
      let token = window.sessionStorage.getItem(OAUTH_TOKEN_KEY);
      let response = await loadConfig(token);
      if (response.oauth && new URLSearchParams(window.location.search).get("code")) {
        const code = new URLSearchParams(window.location.search).get("code") || "";
        const state = new URLSearchParams(window.location.search).get("state") || "";
        const expectedState = window.sessionStorage.getItem(OAUTH_STATE_KEY);
        const verifier = window.sessionStorage.getItem(OAUTH_VERIFIER_KEY);
        const oidc = response.oidc;
        if (!oidc?.configured || !oidc.token_endpoint || !oidc.client_id || !verifier || state !== expectedState) {
          throw new Error("OAuth callback could not be verified. Start sign-in again.");
        }
        const redirectUri = oidc.redirect_uri || `${window.location.origin}${window.location.pathname}`;
        const form = new URLSearchParams({ grant_type: "authorization_code", code, client_id: oidc.client_id, redirect_uri: redirectUri, code_verifier: verifier });
        const exchange = await fetch(oidc.token_endpoint, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: form, credentials: "omit" });
        const tokens = await exchange.json().catch(() => ({})) as { access_token?: string; error_description?: string };
        if (!exchange.ok || !tokens.access_token) throw new Error(tokens.error_description || "OAuth token exchange failed.");
        token = tokens.access_token;
        window.sessionStorage.setItem(OAUTH_TOKEN_KEY, token);
        window.sessionStorage.removeItem(OAUTH_STATE_KEY);
        window.sessionStorage.removeItem(OAUTH_VERIFIER_KEY);
        window.history.replaceState({}, document.title, `${window.location.pathname}${window.location.hash}`);
        response = await loadConfig(token);
      }
      if (!live) return;
      setConfigured(response);
      if (response.oauth) {
        if (response.authentication_error) window.sessionStorage.removeItem(OAUTH_TOKEN_KEY);
        if (response.tenant_id && response.project_id && token && !response.authentication_error) {
          setScope({ tenant_id: response.tenant_id, project_id: response.project_id, principal_id: response.principal_id, access_token: token });
        }
        return;
      }
      const saved = window.localStorage.getItem("scope");
      try {
        const parsed = saved ? JSON.parse(saved) as Scope : null;
        if (parsed?.tenant_id && parsed.project_id) { setScope(parsed); return; }
      } catch { window.localStorage.removeItem("scope"); }
      if (response.tenant_id && response.project_id) {
        const devScope = { tenant_id: response.tenant_id, project_id: response.project_id, principal_id: response.principal_id };
        setScope(devScope); persistDevScope(devScope);
      }
    };
    void load().catch((reason: Error) => live && setError(reason.message));
    return () => { live = false; };
  }, [setScope]);
  if (error) return <main className="boot"><ErrorBox error={new Error(error)} /></main>;
  if (!scope && !configured) return <main className="boot"><Loading label="Resolving project scope" /></main>;
  if (!scope && configured?.oauth) return <OAuthLogin config={configured.oidc} authenticationError={configured.authentication_error} />;
  if (!scope) return <ScopeForm onSelect={(next) => {
    setScope(next);
    persistDevScope(next);
  }} />;
  return <>{children}</>;
}

function OAuthLogin({ config, authenticationError }: { config?: OidcConfig; authenticationError?: string }) {
  const [error, setError] = useState<string | null>(authenticationError || null);
  const begin = async () => {
    try {
      if (!config?.configured || !config.authorization_endpoint || !config.client_id) throw new Error(config?.detail || "The console OIDC client is not configured.");
      const verifier = base64Url(crypto.getRandomValues(new Uint8Array(48)));
      const state = base64Url(crypto.getRandomValues(new Uint8Array(24)));
      const redirectUri = config.redirect_uri || `${window.location.origin}${window.location.pathname}`;
      window.sessionStorage.setItem(OAUTH_VERIFIER_KEY, verifier);
      window.sessionStorage.setItem(OAUTH_STATE_KEY, state);
      const authorization = new URL(config.authorization_endpoint);
      authorization.searchParams.set("response_type", "code");
      authorization.searchParams.set("client_id", config.client_id);
      authorization.searchParams.set("redirect_uri", redirectUri);
      authorization.searchParams.set("scope", config.scopes || "openid profile");
      authorization.searchParams.set("state", state);
      authorization.searchParams.set("code_challenge", await pkceChallenge(verifier));
      authorization.searchParams.set("code_challenge_method", "S256");
      if (config.resource) authorization.searchParams.set("resource", config.resource);
      window.location.assign(authorization.toString());
    } catch (reason) { setError(reason instanceof Error ? reason.message : "OAuth sign-in could not start."); }
  };
  return <main className="boot"><section className="scope-form"><h1>Sign in</h1><p>Authenticate with the configured identity provider to open your server-bound project scope.</p>{error && <ErrorBox error={new Error(error)} />}<button className="primary" type="button" onClick={() => void begin()}>Sign in</button></section></main>;
}

function ScopeForm({ onSelect }: { onSelect: (scope: Scope) => void }) {
  const [tenantId, setTenantId] = useState("");
  const [projectId, setProjectId] = useState("");
  const [principalId, setPrincipalId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const submit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    try {
      // UUID parsing is deliberately local: an invalid scope must never result
      // in an API probe whose response reveals whether another tenant exists.
      const tenant = new URL(`memory://scope/${tenantId}`).pathname.slice(1);
      const project = new URL(`memory://scope/${projectId}`).pathname.slice(1);
      if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(tenant)
          || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(project)) {
        throw new Error("Enter valid tenant and project UUIDs.");
      }
      if (principalId && !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(principalId)) {
        throw new Error("Enter a valid principal UUID or leave it blank.");
      }
      onSelect({ tenant_id: tenantId, project_id: projectId, principal_id: principalId || null });
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Scope is invalid."); }
  };
  return <main className="boot"><form className="scope-form" onSubmit={submit}><h1>Open a project</h1><p>Use a local development scope created by `memory init`. The console will not guess a tenant or project.</p><label>Tenant ID<input aria-label="Tenant ID" value={tenantId} onChange={event => setTenantId(event.target.value.trim())} required /></label><label>Project ID<input aria-label="Project ID" value={projectId} onChange={event => setProjectId(event.target.value.trim())} required /></label><label>Principal ID <span className="muted">optional</span><input aria-label="Principal ID" value={principalId} onChange={event => setPrincipalId(event.target.value.trim())} /></label>{error && <ErrorBox error={new Error(error)} />}<button className="primary" type="submit">Open project</button></form></main>;
}

function TimeCursor() {
  const { asOf, setAsOf } = useAsOf();
  return <label className="time-cursor">As of
    <input aria-label="As of time" type="datetime-local" value={asInput(asOf)} onChange={(event) => {
      setAsOf(event.target.value ? new Date(event.target.value).toISOString() : null);
    }} />
    {asOf && <button className="icon-button" aria-label="Reset timeline cursor" title="Reset timeline cursor" onClick={() => setAsOf(null)}><X size={15} /></button>}
  </label>;
}

export function ConsoleApp({ view }: { view: View }) {
  return <ScopeBootstrap><ConsoleLayout view={view} /></ScopeBootstrap>;
}

function ConsoleLayout({ view }: { view: View }) {
  const { scope, setScope } = useScope();
  const client = useQueryClient();
  const [refreshing, setRefreshing] = useState(false);
  if (!scope) return null;
  const meta = viewMeta[view];
  const refresh = async () => {
    setRefreshing(true);
    try { await client.invalidateQueries(); } finally { window.setTimeout(() => setRefreshing(false), 250); }
  };
  return <div className="console-shell">
    <aside className="sidebar">
      <Link href="/" className="brand"><span className="brand-mark"><Database size={17} /></span><span><strong>Knowledge Console</strong><small>Project memory</small></span></Link>
      <ScopeSwitcher scope={scope} />
      <nav aria-label="Console navigation">
        {(["Knowledge", "Operations", "Governance"] as NavGroup[]).map(group => <div className="nav-group" key={group}><span>{group}</span>{nav.filter(item => item.group === group).map(item => {
          const Icon = item.icon;
          return <Link key={item.view} href={item.href} className={view === item.view ? "nav-link active" : "nav-link"}>
            <Icon size={16} /><span>{item.label}</span>
          </Link>;
        })}</div>)}
      </nav>
      <div className="sidebar-foot"><span className="status-dot" />RLS-scoped{scope.access_token && <button className="icon-button quiet" aria-label="Sign out" title="Sign out" onClick={() => { window.sessionStorage.removeItem(OAUTH_TOKEN_KEY); window.localStorage.removeItem("scope"); setScope(null); }}><LogOut size={15} /></button>}</div>
    </aside>
    <main className="workspace">
      <header className="topbar"><div className="topbar-title"><span className="eyebrow">{meta.section}</span><h1>{view === "knowledge" ? "Knowledge Explorer" : nav.find(item => item.view === view)?.label || "Overview"}</h1><p>{meta.description}</p></div><div className="topbar-actions"><TimeCursor /><button className="icon-button" aria-label="Refresh project data" title="Refresh project data" onClick={() => void refresh()} disabled={refreshing}><RefreshCw className={refreshing ? "spin" : ""} size={15} /></button></div></header>
      <div className="workspace-content">
        {view === "overview" && <Overview scope={scope} />}
        {view === "inbox" && <Inbox scope={scope} />}
        {view === "knowledge" && <Explorer scope={scope} />}
        {view === "procedures" && <Procedures scope={scope} />}
        {view === "graph" && <GraphView scope={scope} />}
        {view === "timeline" && <Timeline scope={scope} />}
        {view === "conflicts" && <Conflicts scope={scope} />}
        {view === "debug" && <Debugger scope={scope} />}
        {view === "health" && <Health scope={scope} />}
        {view === "evals" && <Evals scope={scope} />}
        {view === "settings" && <Settings scope={scope} />}
        {view === "audit" && <Audit scope={scope} />}
        {view === "admin" && <Admin scope={scope} />}
      </div>
    </main>
  </div>;
}

function ScopeSwitcher({ scope }: { scope: Scope }) {
  const { setScope } = useScope();
  const config = useQuery({ queryKey: ["console-config", Boolean(scope.access_token)], queryFn: () => {
    const headers = new Headers();
    if (scope.access_token) headers.set("Authorization", `Bearer ${scope.access_token}`);
    return fetch("/v1/console/config", { credentials: "same-origin", headers }).then(response => response.json() as Promise<{ oauth?: boolean }>);
  }, enabled: !scope.access_token });
  const projects = useQuery({ queryKey: ["project-switcher", scope.tenant_id, scope.principal_id], queryFn: () => read<{ projects: Array<{ id: string; slug: string; name: string }> }>("/v1/projects", scope), enabled: !scope.access_token && !config.data?.oauth });
  if (scope.access_token || config.data?.oauth || !projects.data || projects.data.projects.length < 2) return <div className="scope-label"><span>Project</span><code>{short(scope.project_id, 12)}</code></div>;
  return <label className="scope-label"><span>Project</span><select aria-label="Current project" value={scope.project_id} onChange={event => {
    const next = { ...scope, project_id: event.target.value };
    setScope(next);
    persistDevScope(next);
  }}>{projects.data.projects.map(project => <option key={project.id} value={project.id}>{project.slug}</option>)}</select></label>;
}

function Overview({ scope }: { scope: Scope }) {
  const health = useQuery({ queryKey: ["health", scope], queryFn: () => read<any>("/v1/health/project", scope) });
  const inbox = useQuery({ queryKey: ["inbox", scope], queryFn: () => read<any>("/v1/inbox", scope) });
  const dashboard = useQuery({ queryKey: ["dashboard", scope], queryFn: () => read<Dashboard>("/v1/dashboard", scope, { days: 30 }) });
  if (health.isLoading || inbox.isLoading || dashboard.isLoading) return <Loading />;
  if (health.error || inbox.error || dashboard.error) return <ErrorBox error={health.error || inbox.error || dashboard.error} />;
  if (!dashboard.data) return <Loading />;
  const demand = dashboard.data;
  const gaps = demand.top_questions.filter(item => item.answerability === "no_relevant_evidence" || item.answerability === "partial_support");
  return <section className="overview-grid knowledge-dashboard">
    <section className="metric-band"><div className="metric"><span>Health</span><strong>{health.data.health}<small>/100</small></strong></div><div className="metric"><span>Active knowledge</span><strong>{health.data.counts.active}</strong></div><div className="metric"><span>Questions, {demand.window_days}d</span><strong>{demand.summary.questions}</strong></div><div className="metric"><span>Retrievals, {demand.window_days}d</span><strong>{demand.summary.requests}</strong></div><div className="metric"><span>Review backlog</span><strong>{inbox.data.backlog}</strong></div></section>
    <section className="focus-strip" aria-label="Project attention"><Link className={inbox.data.backlog ? "focus-item attention" : "focus-item"} href="/inbox/"><span>Review work</span><strong>{inbox.data.backlog ? `${inbox.data.backlog} waiting` : "Queue clear"}</strong><small>{inbox.data.oldest_days ? `Oldest candidate ${inbox.data.oldest_days}d` : "No candidate is waiting"}</small></Link><Link className={gaps.length ? "focus-item attention" : "focus-item"} href="/debug/"><span>Evidence gaps</span><strong>{gaps.length ? `${gaps.length} need review` : "Evidence holding"}</strong><small>{gaps.length ? "Inspect unanswered demand" : "No recurring gap recorded"}</small></Link><Link className="focus-item" href="/health/"><span>Knowledge health</span><strong>{health.data.health >= 90 ? "Healthy" : "Needs attention"}</strong><small>{health.data.counts.active} active records in scope</small></Link></section>
    <section className="panel demand-panel"><div className="panel-head"><div><h2>Knowledge demand</h2><p className="muted">Questions sent to this project&apos;s memory in the last {demand.window_days} days.</p></div><Link href="/debug/">Inspect retrieval <ArrowUpRight size={14} /></Link></div><DemandTrend points={demand.trend} /></section>
    <section className="panel dashboard-list"><div className="panel-head"><div><h2>Most asked questions</h2><p className="muted">Demand is grouped by the exact project-scoped request.</p></div><Link href="/audit/">Audit <ArrowUpRight size={14} /></Link></div>{demand.top_questions.length ? demand.top_questions.map(item => <div className="demand-row" key={item.query_text}><div><span className="demand-title" title={item.query_text}>{item.query_text}</span><small>Last asked {formatDate(item.last_asked_at)}</small></div><div className="demand-count"><span className={`answerability answerability-${item.answerability}`}>{answerabilityLabel(item.answerability)}</span><strong>{item.requests}</strong><small>asks</small></div></div>) : <p className="panel-summary">No retrieval events have been recorded for this project.</p>}</section>
    <section className="panel dashboard-list"><div className="panel-head"><div><h2>Knowledge used</h2><p className="muted">The memories actually returned to agents, not a popularity estimate.</p></div><Link href="/knowledge/">Explore <ArrowUpRight size={14} /></Link></div>{demand.top_knowledge.length ? demand.top_knowledge.map(item => <div className={`demand-row ${tierClass(item.tier)}`} key={item.id}><div><span className="demand-title" title={item.title}>{item.title}</span><small>{item.type} · last returned {formatDate(item.last_used_at)}</small></div><div className="demand-count"><strong>{item.requests}</strong><small>returns</small></div></div>) : <p className="panel-summary">No memory has been returned in this period.</p>}</section>
    <section className="panel coverage-panel"><div className="panel-head"><div><h2>Evidence coverage</h2><p className="muted">Whether the retrieval layer found project evidence for each request.</p></div></div><EvidenceCoverage outcomes={demand.outcomes} /><div className="coverage-gaps"><h3>Knowledge gaps</h3>{gaps.length ? gaps.slice(0, 4).map(item => <div className="compact-row" key={item.query_text}><span className="compact-title" title={item.query_text}>{item.query_text}</span><small>{item.requests} asks</small></div>) : <p className="panel-summary">No evidence gaps were recorded in this period.</p>}</div></section>
    <section className="panel overview-panel"><div className="panel-head"><h2>Review queue</h2><Link href="/inbox/">Open inbox <ArrowUpRight size={14} /></Link></div><p className="panel-summary">{inbox.data.health}</p>{inbox.data.items.slice(0, 4).map((item: any) => <div className="compact-row" key={item.ref}><TrustMark tier={item.tier} /><span className="compact-title">{item.title}</span><small>{item.age_days}d</small></div>)}</section>
  </section>;
}

function DemandTrend({ points }: { points: Dashboard["trend"] }) {
  const peak = Math.max(...points.map(point => point.requests), 0);
  const width = 720; const height = 180; const bottom = 150; const left = 28;
  const available = width - left - 8; const slot = available / Math.max(points.length, 1);
  return <div className="demand-chart"><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Retrieval requests per day"><line className="timeline-axis" x1={left} y1={bottom} x2={width - 8} y2={bottom} />{points.map((point, index) => { const value = peak ? point.requests / peak : 0; const barHeight = Math.max(point.requests ? 3 : 0, value * 112); const x = left + index * slot + Math.max(1, slot * 0.18); const barWidth = Math.max(2, slot * 0.64); return <g key={point.date}><title>{new Date(`${point.date}T00:00:00`).toLocaleDateString()}: {point.requests} retrievals, {point.questions} questions</title><rect className="demand-bar" x={x} y={bottom - barHeight} width={barWidth} height={barHeight} rx="1" /></g>; })}<text x={left} y="170">{points[0] ? new Date(`${points[0].date}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : ""}</text><text x={width - 8} y="170" textAnchor="end">today</text><text x={left} y="18">{peak ? `${peak} peak requests` : "No retrievals"}</text></svg></div>;
}

function EvidenceCoverage({ outcomes }: { outcomes: Dashboard["outcomes"] }) {
  const total = outcomes.reduce((sum, item) => sum + item.count, 0);
  if (!outcomes.length) return <p className="panel-summary">No evidence decisions have been recorded for this period.</p>;
  return <div className="coverage-list">{outcomes.map(item => <div className="coverage-row" key={item.status}><div><span>{answerabilityLabel(item.status)}</span><strong>{item.count}</strong></div><span className={`coverage-track answerability-${item.status}`}><i style={{ width: `${total ? (item.count / total) * 100 : 0}%` }} /></span></div>)}</div>;
}

function Inbox({ scope }: { scope: Scope }) {
  const client = useQueryClient();
  const [cursor, setCursor] = useState(0);
  const [rejecting, setRejecting] = useState<string | null>(null);
  const [lastDecision, setLastDecision] = useState<{ ref: string; action: string } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const inbox = useQuery({ queryKey: ["inbox", scope], queryFn: () => read<any>("/v1/inbox", scope) });
  const act = async (ref: string, action: string, note = "") => {
    await write("/v1/inbox/review", scope, { ref, action, to_tier: action === "promote" ? "observed" : undefined, note });
    if (action === "promote" || action === "reject") {
      setLastDecision({ ref, action });
      setNotice(action === "promote" ? "Accepted candidate. Undo is available for 10 seconds." : "Archived candidate. Undo is available for 10 seconds.");
    }
    await client.invalidateQueries({ queryKey: ["inbox", scope] });
  };
  const undo = async () => {
    if (!lastDecision) return;
    try {
      await write("/v1/inbox/review", scope, { ref: lastDecision.ref, action: "undo" });
      setNotice("Undone. Candidate returned to the review queue.");
      setLastDecision(null);
      await client.invalidateQueries({ queryKey: ["inbox", scope] });
    } catch (reason) {
      setNotice(reason instanceof Error ? `Could not undo: ${reason.message}` : "Could not undo the last decision.");
    }
  };
  useEffect(() => {
    if (!lastDecision) return;
    const timer = window.setTimeout(() => { setLastDecision(null); setNotice(null); }, 10_000);
    return () => window.clearTimeout(timer);
  }, [lastDecision]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.target as HTMLElement)?.matches("input, textarea, select, button")) return;
      const items = inbox.data?.items || [];
      if (event.key === "j") setCursor(value => Math.min(value + 1, Math.max(items.length - 1, 0)));
      if (event.key === "k") setCursor(value => Math.max(value - 1, 0));
       if (event.key === "a" && items[cursor]?.kind !== "conflict") void act(items[cursor].ref, "promote");
       if (event.key === "r" && items[cursor]?.kind !== "conflict") setRejecting(items[cursor].ref);
       if (event.key === "u") void undo();
    };
    window.addEventListener("keydown", onKey); return () => window.removeEventListener("keydown", onKey);
  }, [cursor, inbox.data, scope, lastDecision]);
  if (inbox.isLoading) return <Loading />;
  if (inbox.error) return <ErrorBox error={inbox.error} />;
  return <section>{notice && <div id="toast" className="notice success" role="status"><Check size={16} />{notice}{lastDecision && <button onClick={() => void undo()}>Undo</button>}</div>}<div className="section-head"><div><p className="muted">{inbox.data.backlog} candidates, oldest {inbox.data.oldest_days}d</p><h2>Review queue</h2></div><span className="status-pill">{inbox.data.health}</span></div><div className="review-summary" aria-label="Review queue summary"><div><span>Waiting</span><strong>{inbox.data.backlog}</strong></div><div><span>Oldest</span><strong>{inbox.data.oldest_days}d</strong></div><div><span>Queue state</span><strong>{inbox.data.health}</strong></div></div>
    <div className="queue">{inbox.data.items.length === 0 ? <div className="empty">No candidates waiting.</div> : inbox.data.items.map((item: any, index: number) => <article tabIndex={0} key={item.ref} className={`inbox-item ${tierClass(item.tier)} ${cursor === index ? "cursor" : ""}`} onFocus={() => setCursor(index)}><div className="item-top"><span className="kind">{item.kind}</span><span>{item.type}</span><span>{item.age_days}d old</span></div><h3>{item.title}</h3><p>{item.digest}</p>{item.why && <div className="flag"><AlertTriangle size={14} />{Array.isArray(item.why) ? item.why.join(", ") : JSON.stringify(item.why)}</div>}<div className="item-foot"><code>{item.source}{item.source_uri ? `:${item.source_uri}` : ""}</code>{item.kind === "conflict" ? <Link href="/conflicts/">Resolve conflict</Link> : <div className="actions"><button className="primary" onClick={() => void act(item.ref, "promote")}>Accept</button><button onClick={() => setRejecting(item.ref)}>Reject</button></div>}</div></article>)}</div>
    {rejecting && <div className="modal-backdrop" role="presentation"><dialog open><h2>Archive candidate</h2><p>Nothing is deleted. Select the reason recorded in the audit trail.</p><div className="actions">{["noise", "wrong", "already known", "too specific", "unsafe"].map(reason => <button key={reason} onClick={() => { void act(rejecting, "reject", reason); setRejecting(null); }}>{reason}</button>)}</div><button className="quiet" onClick={() => setRejecting(null)}>Cancel</button></dialog></div>}
  </section>;
}

function Explorer({ scope }: { scope: Scope }) {
  const { asOf } = useAsOf();
  const [filters, setFilters] = useState(() => ({ q: "", types: "", tiers: "", statuses: "", sort: "recorded_at", direction: "desc" }));
  const [expanded, setExpanded] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [viewName, setViewName] = useState("");
  const [actionError, setActionError] = useState<Error | null>(null);
  const [actionNotice, setActionNotice] = useState<string | null>(null);
  const params = { ...filters, as_of: asOf, limit: 100 };
  const explorer = useQuery({ queryKey: ["explorer", scope, params], queryFn: () => read<{ items: ExplorerMemory[]; total: number }>("/v1/explorer", scope, params) });
  const views = useQuery({ queryKey: ["saved-views", scope], queryFn: () => read<{ views: Array<{ id: string; name: string; filters: Record<string, string> }> }>("/v1/console/views", scope) });
  const client = useQueryClient();
  const saveView = async () => { if (!viewName.trim()) return; await write("/v1/console/views", scope, { name: viewName.trim(), filters }); setViewName(""); await client.invalidateQueries({ queryKey: ["saved-views", scope] }); };
  const act = async (action: "archive" | "pin" | "unpin" | "reembed") => {
    if (!selected.length || (action === "reembed" && selected.length !== 1)) return;
    setActionError(null); setActionNotice(null);
    try {
      await write("/v1/console/memories/actions", scope, { refs: selected, action });
      setActionNotice(`${action === "archive" ? "Archived" : action === "reembed" ? "Re-embedded" : action === "pin" ? "Pinned" : "Unpinned"} ${selected.length} memor${selected.length === 1 ? "y" : "ies"}.`);
      if (action === "archive" || action === "reembed") setSelected([]);
      await client.invalidateQueries({ queryKey: ["explorer", scope] });
      await client.invalidateQueries({ queryKey: ["memory-detail", scope] });
    } catch (reason) { setActionError(reason instanceof Error ? reason : new Error("Memory action failed")); }
  };
  const columns = useMemo<ColumnDef<ExplorerMemory>[]>(() => [
    { id: "trust", header: "Trust", cell: ({ row }) => <span className="selection-cell"><input type="checkbox" aria-label={`Select ${row.original.title}`} checked={selected.includes(row.original.id)} onClick={event => event.stopPropagation()} onChange={event => setSelected(current => event.target.checked ? [...new Set([...current, row.original.id])] : current.filter(id => id !== row.original.id))} /><TrustMark tier={row.original.tier} /></span> },
    { accessorKey: "type", header: "Type" }, { accessorKey: "title", header: "Title" }, { accessorKey: "scope_kind", header: "Scope" },
    { id: "valid", header: "Valid", cell: ({ row }) => <span>{formatDate(row.original.valid_from)}{row.original.valid_until ? ` to ${formatDate(row.original.valid_until)}` : " to open"}</span> },
    { id: "source", header: "Source", cell: ({ row }) => <code>{row.original.source_uri || row.original.source_type}</code> },
    { id: "last", header: "Last used", cell: ({ row }) => formatDate(row.original.last_accessed_at) },
    { accessorKey: "retrieval_count", header: "Uses" }, { accessorKey: "token_cost", header: "Tokens" }, { accessorKey: "status", header: "Status" }
  ], [selected]);
  const table = useReactTable({ data: explorer.data?.items || [], columns, getCoreRowModel: getCoreRowModel() });
  const parent = useRef<HTMLDivElement>(null);
  const rows = table.getRowModel().rows;
  const virtual = useVirtualizer({ count: rows.length, getScrollElement: () => parent.current, estimateSize: () => 48, getItemKey: index => rows[index]?.id || index, measureElement: element => element.getBoundingClientRect().height, overscan: 12 });
  const setFilter = (key: keyof typeof filters, value: string) => { setExpanded(null); setSelected([]); setFilters(current => ({ ...current, [key]: value })); };
  const resetFilters = () => { setExpanded(null); setSelected([]); setFilters({ q: "", types: "", tiers: "", statuses: "", sort: "recorded_at", direction: "desc" }); };
  const applyView = (view: { filters: Record<string, string> }) => { setExpanded(null); setSelected([]); setFilters(current => ({ ...current, ...view.filters })); };
  const toggleExpanded = (id: string) => setExpanded(current => current === id ? null : id);
  return <section className="explorer"><div className="section-head explorer-head"><div><p className="muted">Search, inspect, and maintain project memory.</p><h2>Memory explorer</h2></div><span className="status-pill">{explorer.data?.total ?? 0} scoped</span></div><div className="filterbar"><label><Search size={15}/><input aria-label="Search knowledge" value={filters.q} onChange={event => setFilter("q", event.target.value)} placeholder="Search knowledge" /></label><select aria-label="Memory type" value={filters.types} onChange={event => setFilter("types", event.target.value)}><option value="">All types</option><option value="decision">Decision</option><option value="procedure">Procedure</option><option value="constraint">Constraint</option><option value="episode">Episode</option></select><select aria-label="Trust tier" value={filters.tiers} onChange={event => setFilter("tiers", event.target.value)}><option value="">All trust</option>{["authoritative", "verified", "observed", "inferred", "untrusted"].map(tier => <option key={tier}>{tier}</option>)}</select><select aria-label="Memory status" value={filters.statuses} onChange={event => setFilter("statuses", event.target.value)}><option value="">All status</option>{["active", "quarantined", "archived", "superseded"].map(status => <option key={status}>{status}</option>)}</select><select aria-label="Sort memories" value={filters.sort} onChange={event => setFilter("sort", event.target.value)}><option value="recorded_at">Recorded</option><option value="uses">Uses</option><option value="last_used">Last used</option><option value="tokens">Tokens</option></select><button className="icon-button" aria-label={filters.direction === "desc" ? "Sort descending" : "Sort ascending"} title={filters.direction === "desc" ? "Sort descending" : "Sort ascending"} onClick={() => setFilter("direction", filters.direction === "desc" ? "asc" : "desc")}>{filters.direction === "desc" ? <ArrowDown size={15} /> : <ArrowUp size={15} />}</button><button className="icon-button" aria-label="Reset explorer filters" title="Reset explorer filters" onClick={resetFilters}><RotateCcw size={15} /></button></div>
    {selected.length > 0 && <div className="selection-actions"><span>{selected.length} selected</span><button className="icon-button" aria-label="Archive selected memories" title="Archive selected memories" onClick={() => void act("archive")}><Archive size={15} /></button><button className="icon-button" aria-label="Pin selected memories" title="Pin selected memories" onClick={() => void act("pin")}><Pin size={15} /></button><button className="icon-button" aria-label="Unpin selected memories" title="Unpin selected memories" onClick={() => void act("unpin")}><PinOff size={15} /></button>{selected.length === 1 && <button className="icon-button" aria-label="Re-embed selected memory" title="Re-embed selected memory" onClick={() => void act("reembed")}><RefreshCw size={15} /></button>}<button className="quiet" onClick={() => setSelected([])}>Clear</button></div>}
    {actionNotice && <div className="notice success" role="status"><Check size={16} />{actionNotice}</div>}{actionError && <ErrorBox error={actionError} />}
    <div className="saved-row"><span>Saved views</span>{views.data?.views.map(view => <button className="chip" key={view.id} onClick={() => applyView(view)}>{view.name}</button>)}<input aria-label="Saved view name" value={viewName} onChange={event => setViewName(event.target.value)} placeholder="Name this view" /><button onClick={() => void saveView()}>Save view</button></div>
    {explorer.isLoading ? <Loading /> : explorer.error ? <ErrorBox error={explorer.error} /> : rows.length === 0 ? <div className="empty">No memories match this scoped view.</div> : <><div className="virtual-table" ref={parent} role="region" aria-label="Memory results"><div className="explorer-grid"><div className="data-header">{table.getHeaderGroups().map(group => group.headers.map(header => <span key={header.id}>{flexRender(header.column.columnDef.header, header.getContext())}</span>))}</div><div className="virtual-body" style={{ height: `${virtual.getTotalSize()}px` }}>{virtual.getVirtualItems().map(item => { const row = rows[item.index]; const isExpanded = expanded === row.original.id; return <div key={row.id} ref={virtual.measureElement} data-index={item.index} role="button" tabIndex={0} aria-expanded={isExpanded} className={`data-row ${isExpanded ? "selected" : ""}`} style={{ transform: `translateY(${item.start}px)` }} onClick={() => toggleExpanded(row.original.id)} onKeyDown={event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggleExpanded(row.original.id); } }}>{row.getVisibleCells().map(cell => <span key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</span>)}{isExpanded && <div className="row-detail"><p>{row.original.digest}</p><code>{row.original.source_type}:{row.original.source_uri || "-"}@{short(row.original.source_version)}</code><span>{row.original.active_at_as_of ? "Active at cursor" : "Outside cursor"}</span></div>}</div>; })}</div></div></div>{expanded && <MemoryDetail scope={scope} refId={expanded} />}</>}</section>;
}

function MemoryDetail({ scope, refId }: { scope: Scope; refId: string }) {
  const detail = useQuery({ queryKey: ["memory-detail", scope, refId], queryFn: () => read<any>("/v1/explain", scope, { ref: refId }) });
  const client = useQueryClient();
  const [tab, setTab] = useState<"content" | "provenance" | "history" | "relations" | "usage" | "raw">("content");
  const [actionError, setActionError] = useState<Error | null>(null);
  const [actionNotice, setActionNotice] = useState<string | null>(null);
  if (detail.isLoading) return <Loading label="Loading memory detail" />;
  if (detail.error) return <ErrorBox error={detail.error} />;
  const data = detail.data;
  if (!data) return null;
  const memory = data.memory;
  const act = async (action: "archive" | "pin" | "unpin" | "reembed") => {
    setActionError(null); setActionNotice(null);
    try { await write("/v1/console/memories/actions", scope, { refs: [refId], action }); setActionNotice(`${action === "archive" ? "Archived" : action === "reembed" ? "Re-embedded" : action === "pin" ? "Pinned" : "Unpinned"} memory.`); await client.invalidateQueries({ queryKey: ["memory-detail", scope, refId] }); await client.invalidateQueries({ queryKey: ["explorer", scope] }); } catch (reason) { setActionError(reason instanceof Error ? reason : new Error("Memory action failed")); }
  };
  const tabs = [["content", "Content"], ["provenance", "Provenance"], ["history", "History"], ["relations", "Relations"], ["usage", "Usage"], ["raw", "Raw"]] as const;
  return <section className="panel memory-detail"><div className="panel-head"><div><p className="muted">{memory.type} · {memory.status} · {memory.tier}</p><h2>{memory.title}</h2></div><div className="detail-actions"><TrustMark tier={memory.tier} /><button className="icon-button" aria-label={memory.pinned ? "Unpin memory" : "Pin memory"} title={memory.pinned ? "Unpin memory" : "Pin memory"} onClick={() => void act(memory.pinned ? "unpin" : "pin")}>{memory.pinned ? <PinOff size={15} /> : <Pin size={15} />}</button><button className="icon-button" aria-label="Re-embed memory" title="Re-embed memory" onClick={() => void act("reembed")}><RefreshCw size={15} /></button><button className="icon-button" aria-label="Archive memory" title="Archive memory" onClick={() => void act("archive")}><Archive size={15} /></button></div></div>{actionNotice && <div className="notice success" role="status"><Check size={16} />{actionNotice}</div>}{actionError && <ErrorBox error={actionError} />}<div className="detail-tabs" role="tablist" aria-label="Memory detail sections">{tabs.map(([id, label]) => <button key={id} role="tab" aria-selected={tab === id} className={tab === id ? "active" : ""} onClick={() => setTab(id)}>{label}</button>)}</div>{tab === "content" && <pre>{memory.content}</pre>}{tab === "provenance" && <dl className="detail-grid"><div><dt>Source</dt><dd><code>{data.provenance}</code></dd></div><div><dt>Recorded</dt><dd>{formatDate(memory.recorded_at)}</dd></div><div><dt>Validity</dt><dd><code>{memory.valid_at}</code></dd></div><div><dt>Sensitivity</dt><dd>{memory.sensitivity}</dd></div>{data.provenance_url && <div><dt>Resolved source</dt><dd><a className="source-link" href={data.provenance_url} target="_blank" rel="noreferrer">Open exact version</a></dd></div>}</dl>}{tab === "history" && <><h3>Versions</h3>{data.versions.length ? <table><thead><tr><th>Version</th><th>Operation</th><th>Changed</th></tr></thead><tbody>{data.versions.map((version: any) => <tr key={version.version}><td>{version.version}</td><td>{version.operation}</td><td>{formatDate(version.changed_at)}</td></tr>)}</tbody></table> : <p className="muted">No version history is available.</p>}<h3>Supersessions</h3>{data.supersessions.length ? data.supersessions.map((item: any) => <div className="compact-row" key={`${item.old_id}-${item.new_id}`}><code>{short(item.old_id)} → {short(item.new_id)}</code><span>{item.reason || "No reason recorded"}</span></div>) : <p className="muted">No supersession links.</p>}</>}{tab === "relations" && <><h3>Entities</h3>{data.entities.length ? <div className="suggestions">{data.entities.map((entity: any) => <span className="chip" key={entity.id}>{entity.canonical_name}<small>{entity.kind}</small></span>)}</div> : <p className="muted">No extracted entities.</p>}<h3>Evidence relationships</h3>{data.relations.length ? <table><thead><tr><th>From</th><th>Relation</th><th>To</th><th>Confidence</th></tr></thead><tbody>{data.relations.map((relation: any) => <tr key={relation.id}><td>{relation.source_name}</td><td><code>{relation.relation}</code></td><td>{relation.target_name}</td><td>{relation.confidence}</td></tr>)}</tbody></table> : <p className="muted">This memory has not asserted a relationship.</p>}</>}{tab === "usage" && <dl className="detail-grid"><div><dt>Retrievals</dt><dd>{data.usage.retrievals}</dd></div><div><dt>Context packs</dt><dd>{data.usage.packs}</dd></div><div><dt>Principals</dt><dd>{data.usage.principals}</dd></div><div><dt>Last retrieved</dt><dd>{formatDate(data.usage.last_seen || memory.last_accessed_at)}</dd></div><div><dt>Token cost</dt><dd>{memory.token_cost}</dd></div></dl>}{tab === "raw" && <pre>{JSON.stringify(memory, null, 2)}</pre>}</section>;
}

function Procedures({ scope }: { scope: Scope }) {
  const { asOf } = useAsOf();
  const [selected, setSelected] = useState<string | null>(null);
  const procedures = useQuery({ queryKey: ["procedures", scope, asOf], queryFn: () => read<{ procedures: any[] }>("/v1/procedures", scope, { as_of: asOf }) });
  if (procedures.isLoading) return <Loading />;
  if (procedures.error) return <ErrorBox error={procedures.error} />;
  return <section><div className="section-head"><div><p className="muted">Reviewed procedures and their observed use.</p><h2>Procedures</h2></div><span className="status-pill">{procedures.data?.procedures.length || 0} active</span></div>{procedures.data?.procedures.length ? <section className="panel"><table><thead><tr><th>Trust</th><th>Procedure</th><th>Source</th><th>Uses</th><th>Last used</th></tr></thead><tbody>{procedures.data.procedures.map(item => <tr className="clickable" key={item.id} onClick={() => setSelected(item.id)}><td><TrustMark tier={item.tier} />{item.tier}</td><td><strong>{item.title}</strong><br /><small className="muted">{item.digest}</small></td><td><code>{item.source_uri || item.source_type}</code></td><td>{item.retrieval_count}</td><td>{formatDate(item.last_accessed_at)}</td></tr>)}</tbody></table></section> : <div className="empty">No active procedures.</div>}{selected && <MemoryDetail scope={scope} refId={selected} />}</section>;
}

function GraphView({ scope }: { scope: Scope }) {
  const { asOf } = useAsOf();
  const [query, setQuery] = useState("");
  const [focus, setFocus] = useState<string | null>(null);
  const [focusDismissed, setFocusDismissed] = useState(false);
  const graph = useQuery({ queryKey: ["graph", scope, query, focus, asOf], queryFn: () => read<{ nodes: GraphNode[]; edges: GraphEdge[]; suggestions: GraphNode[] }>("/v1/graph", scope, { q: query, entity_id: focus, as_of: asOf }) });
  useEffect(() => {
    if (focus || focusDismissed || query.trim()) return;
    const connected = graph.data?.suggestions.find(node => (node.relationship_count || 0) > 0);
    if (connected) setFocus(connected.id);
  }, [focus, focusDismissed, graph.data, query]);
  if (graph.isLoading) return <Loading />;
  if (graph.error) return <ErrorBox error={graph.error} />;
  if (!graph.data) return <Loading label="Loading graph" />;
  const graphData = graph.data;
  const hasRelationships = graphData.edges.length > 0 && graphData.nodes.length > 1;
  const focusName = graphData.nodes.find(node => node.id === focus)?.canonical_name || "This entity";
  return <section><div className="section-head"><div><p className="muted">Two-hop, scoped neighbourhood</p><h2>Entity graph</h2></div><label className="search-field"><Search size={15}/><input aria-label="Find an entity" value={query} onChange={event => { setQuery(event.target.value); setFocus(null); setFocusDismissed(false); }} placeholder="Find an entity" /></label></div>{!focus ? <div className="panel graph-picker"><h3>Choose an entity</h3><p>The graph opens on connected evidence and never renders an unbounded project graph.</p>{graphData.suggestions.length ? <div className="suggestions">{graphData.suggestions.map(node => <button key={node.id} onClick={() => setFocus(node.id)}>{node.canonical_name}<small>{node.kind}{node.relationship_count ? ` · ${node.relationship_count} links` : ""}</small></button>)}</div> : <div className="empty compact-empty">No entities match this scoped search.</div>}</div> : <><div className="graph-toolbar"><button onClick={() => { setFocus(null); setFocusDismissed(true); }}>Back to suggestions</button><span>{graphData.nodes.length} nodes, {graphData.edges.length} edges</span></div>{hasRelationships ? <><GraphCanvas nodes={graphData.nodes} edges={graphData.edges} /><GraphFallback nodes={graphData.nodes} edges={graphData.edges} /></> : <section className="panel graph-empty-state"><Network size={20} /><div><h3>No recorded relationships</h3><p>{focusName} is in scope, but no active relationship evidence is available at this cursor.</p></div><button onClick={() => { setFocus(null); setFocusDismissed(true); }}>Choose another entity</button></section>}</>}</section>;
}

function GraphCanvas({ nodes, edges }: { nodes: GraphNode[]; edges: GraphEdge[] }) {
  const container = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let cancelled = false; let renderer: { kill: () => void } | undefined;
    void Promise.all([import("graphology"), import("sigma")]).then(([graphology, sigma]) => {
      if (cancelled || !container.current) return;
      const Graph = graphology.default;
      const Sigma = sigma.default;
      const graph = new Graph();
      nodes.forEach((node, index) => graph.addNode(node.id, { label: node.canonical_name, x: Math.cos(index * 2.4), y: Math.sin(index * 2.4), size: 8, color: "#57A6FF" }));
      edges.forEach(edge => { if (graph.hasNode(edge.source_id) && graph.hasNode(edge.target_id)) graph.addEdgeWithKey(edge.id, edge.source_id, edge.target_id, { label: edge.relation, size: Math.max(1, edge.confidence * 4), color: edge.proposed ? "#E3B341" : "#9AA7B4" }); });
      renderer = new Sigma(graph, container.current!);
    });
    return () => { cancelled = true; renderer?.kill(); };
  }, [nodes, edges]);
  return <div ref={container} className="graph-canvas" aria-label="Interactive entity graph" role="img" />;
}

function GraphFallback({ nodes, edges }: { nodes: GraphNode[]; edges: GraphEdge[] }) {
  const labels = new Map(nodes.map(node => [node.id, node.canonical_name]));
  return <section className="panel graph-fallback"><h3>Relationship table</h3><table><thead><tr><th>From</th><th>Relation</th><th>To</th><th>Trust</th><th>Evidence</th></tr></thead><tbody>{edges.map(edge => <tr key={edge.id}><td>{labels.get(edge.source_id)}</td><td>{edge.relation}{edge.proposed && <span className="proposed"> proposed</span>}</td><td>{labels.get(edge.target_id)}</td><td><TrustMark tier={edge.tier} />{edge.tier}</td><td><code>{short(edge.evidence_memory_id)}</code></td></tr>)}</tbody></table></section>;
}

function Timeline({ scope }: { scope: Scope }) {
  const { asOf, setAsOf } = useAsOf();
  const timeline = useQuery({ queryKey: ["timeline", scope, asOf], queryFn: () => read<any>("/v1/timeline", scope, { as_of: asOf, limit: 250 }) });
  if (timeline.isLoading) return <Loading />;
  if (timeline.error) return <ErrorBox error={timeline.error} />;
  const all = [...timeline.data.valid_lane, ...timeline.data.recorded_lane];
  const times = all.flatMap((item: any) => [new Date(item.valid_from || item.recorded_at).getTime(), item.valid_until ? new Date(item.valid_until).getTime() : Date.now()]).filter(Number.isFinite);
  const min = Math.min(...times, Date.now() - 86_400_000); const max = Math.max(...times, Date.now());
  const current = asOf ? new Date(asOf).getTime() : max;
  return <section className="timeline"><div className="section-head"><div><p className="muted">Valid time and record time stay separate.</p><h2>Bi-temporal timeline</h2></div></div><div className="timeline-cursor-control"><label className="range-label">Cursor<input type="range" min="0" max="1000" value={Math.round(((current - min) / Math.max(max - min, 1)) * 1000)} onChange={event => setAsOf(new Date(min + (Number(event.target.value) / 1000) * (max - min)).toISOString())} /></label><div className="timeline-range-meta"><span>{formatDate(new Date(min).toISOString())}</span><span>{formatDate(new Date(current).toISOString())}</span><span>{formatDate(new Date(max).toISOString())}</span></div></div><TimelineLane label="Valid time" items={timeline.data.valid_lane} min={min} max={max} /><TimelineLane label="Recorded time" items={timeline.data.recorded_lane} min={min} max={max} /></section>;
}

function TimelineLane({ label, items, min, max }: { label: string; items: any[]; min: number; max: number }) {
  const span = Math.max(max - min, 1);
  const ordered = [...items].sort((left, right) => new Date(left.valid_from || left.recorded_at).getTime() - new Date(right.valid_from || right.recorded_at).getTime());
  return <section className="timeline-lane"><div className="timeline-lane-head"><h3>{label}</h3><span>{items.length} memories</span></div>{ordered.length ? <div className="timeline-list" role="list" aria-label={`${label} events`}>{ordered.map(item => { const from = new Date(item.valid_from || item.recorded_at).getTime(); const until = item.valid_until ? new Date(item.valid_until).getTime() : max; const left = Math.max(0, Math.min(100, ((from - min) / span) * 100)); const right = Math.max(left + 1.2, Math.min(100, ((until - min) / span) * 100)); return <article className="timeline-event" role="listitem" key={item.id}><div className="timeline-event-title"><TrustMark tier={item.tier} /><span title={item.title}>{item.title}</span><small>{item.type || "memory"}</small></div><div className="timeline-track" aria-label={`${item.title}: ${formatDate(item.valid_from || item.recorded_at)}`}><span className={item.active_at_as_of ? "timeline-segment active" : "timeline-segment"} style={{ left: `${left}%`, width: `${right - left}%` }} /></div><time>{formatDate(item.valid_from || item.recorded_at)}</time></article>; })}</div> : <div className="empty compact-empty">No events are available in this lane.</div>}</section>;
}

function Conflicts({ scope }: { scope: Scope }) {
  const client = useQueryClient();
  const conflicts = useQuery({ queryKey: ["conflicts", scope], queryFn: () => read<any>("/v1/conflicts", scope) });
  const resolve = async (ref: string, resolution: string) => { await write("/v1/inbox/review", scope, { ref, action: "resolve", note: resolution }); await client.invalidateQueries({ queryKey: ["conflicts", scope] }); };
  if (conflicts.isLoading) return <Loading />; if (conflicts.error) return <ErrorBox error={conflicts.error} />;
  return <section><div className="section-head"><div><p className="muted">Both sides stay visible until a recorded decision resolves them.</p><h2>Contested points</h2></div><span className="status-pill">{conflicts.data.count} open</span></div>{conflicts.data.conflicts.length === 0 ? <div className="empty">No unresolved conflicts.</div> : conflicts.data.conflicts.map((conflict: any) => { const [a, b] = conflict.sides; return <article className="conflict" key={conflict.conflict_id}><div><TrustMark tier={a?.trust} /><h3>{a?.title || "Missing side"}</h3><p>{a?.digest}</p><code>{a?.ref || "-"}</code></div><div className="versus">vs<br /><small>{conflict.kind}</small></div><div><TrustMark tier={b?.trust} /><h3>{b?.title || "Missing side"}</h3><p>{b?.digest}</p><code>{b?.ref || "-"}</code></div><div className="conflict-actions"><button className="primary" onClick={() => void resolve(conflict.conflict_id, "A supersedes B")}>A supersedes B</button><button onClick={() => void resolve(conflict.conflict_id, "B supersedes A")}>B supersedes A</button><button onClick={() => void resolve(conflict.conflict_id, "Both valid in distinct scope or time")}>Both valid</button></div></article>; })}</section>;
}

function Debugger({ scope }: { scope: Scope }) {
  const { asOf } = useAsOf();
  const [task, setTask] = useState("why did we choose pgvector?");
  const [pack, setPack] = useState<any>(null); const [caseTemplate, setCaseTemplate] = useState<any>(null); const [labels, setLabels] = useState<Record<string, 1 | 2 | 3 | "forbidden">>({}); const [error, setError] = useState<Error | null>(null);
  const run = async () => { setError(null); setCaseTemplate(null); setLabels({}); try { const next = await write<any>("/v1/context", scope, { task, token_budget: 4000, as_of: asOf }); setPack(next); setCaseTemplate(await read<any>("/v1/eval/case-template", scope, { pack_id: next.pack_id })); } catch (reason) { setError(reason as Error); } };
  const setLabel = (ref: string, label: 1 | 2 | 3 | "forbidden") => setLabels(current => current[ref] === label ? Object.fromEntries(Object.entries(current).filter(([key]) => key !== ref)) : { ...current, [ref]: label });
  const exportCase = () => { if (!pack || !caseTemplate) return; const reviewed = { ...caseTemplate, case: { ...caseTemplate.case, expect: caseTemplate.candidates.filter((candidate: any) => typeof labels[candidate.ref] === "number").map((candidate: any) => ({ key: candidate.key, hash: candidate.hash, grade: labels[candidate.ref] })), forbid: [], forbidden_memory_ids: caseTemplate.candidates.filter((candidate: any) => labels[candidate.ref] === "forbidden").map((candidate: any) => ({ key: candidate.key, hash: candidate.hash })) } }; const blob = new Blob([JSON.stringify(reviewed, null, 2)], { type: "application/json" }); const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = `eval-case-${pack.pack_id}.json`; link.click(); URL.revokeObjectURL(url); };
  const answerability = pack?.answerability || {};
  const evidenceStatus = answerability.status || "unknown";
  const evidenceLabel = evidenceStatus === "no_relevant_evidence"
    ? "No relevant project evidence"
    : evidenceStatus === "evidence_not_included"
      ? "Evidence not included"
      : evidenceStatus === "partial_support"
        ? "Partial project evidence"
      : evidenceStatus === "supported"
        ? "Project evidence found"
        : "Evidence decision unavailable";
  return <section>
    <div className="section-head"><div><p className="muted">Replayable ranking evidence.</p><h2>Retrieval debugger</h2></div></div>
    <div className="debug-run"><input type="search" value={task} onChange={event => setTask(event.target.value)} aria-label="Task to debug" /><button className="primary" onClick={() => void run()}>Run</button></div>
    {error && <ErrorBox error={error} />}
    {pack && <div className="debug-stages">
      <section className={`stage evidence-decision ${evidenceStatus}`}>
        <div className="stage-heading"><h3>Evidence decision</h3><span className="status-pill">{evidenceLabel}</span></div>
        <p>{pack.notice || answerability.reason}</p>
        <dl className="evidence-metrics">
          <div><dt>Candidates examined</dt><dd>{answerability.considered_count ?? "-"}</dd></div>
          <div><dt>Evidence in pack</dt><dd>{pack.evidence_count ?? answerability.evidence_count ?? 0}</dd></div>
          <div><dt>Reranker</dt><dd>{pack.rerank?.applied ? `applied to ${pack.rerank.scored}` : "not applied"}</dd></div>
        </dl>
        {answerability.reason && <p className="muted">{answerability.reason}</p>}
      </section>
      <section className="stage"><h3>Plan</h3><code>intent={pack.plan.intent} matched on {pack.plan.matched || "fallback"}</code><pre>{JSON.stringify(pack.plan, null, 2)}</pre></section>
      <section className="stage"><h3>Arms</h3><table><tbody>{Object.entries(pack.timings_ms).map(([stage, time]) => <tr key={stage}><th>{stage}</th><td>{String(time)} ms</td></tr>)}</tbody></table></section>
      <section className="stage"><h3>Pack - {pack.budget.used} / {pack.budget.effective} tokens</h3>{Object.entries(pack.sections).map(([section, items]) => <div key={section}><h4>{section}</h4>{(items as any[]).map(item => <div className={`compact-row ${tierClass(item.trust)}`} key={item.ref || item.conflict_id}><span className="compact-title">{item.title || item.kind}</span><small>{item.score?.toFixed?.(3) || "contested"}</small></div>)}</div>)}</section>
      <section className="stage"><h3>Dropped</h3>{pack.dropped.length ? <div className="dropped-list">{pack.dropped.map((item: any) => <div className="dropped-row" key={item.id}><span className="dropped-title" title={item.title}>{item.title}</span><code className="dropped-reason" title={item.reason}>{item.reason}</code></div>)}</div> : <p>Nothing was dropped.</p>}</section>
      {caseTemplate && <section className="stage eval-review"><h3>Reviewed case</h3><div className="eval-candidates">{caseTemplate.candidates.map((candidate: any) => <div className="eval-candidate" key={candidate.ref}><span className="compact-title" title={candidate.title}>{candidate.title}</span><div className="segmented-control" aria-label={`Evaluation label for ${candidate.title}`}><button className={labels[candidate.ref] === 3 ? "selected" : ""} onClick={() => setLabel(candidate.ref, 3)}>Answer</button><button className={labels[candidate.ref] === 2 ? "selected" : ""} onClick={() => setLabel(candidate.ref, 2)}>Related</button><button className={labels[candidate.ref] === 1 ? "selected" : ""} onClick={() => setLabel(candidate.ref, 1)}>Context</button><button className={labels[candidate.ref] === "forbidden" ? "danger selected" : "danger"} onClick={() => setLabel(candidate.ref, "forbidden")}>Forbidden</button></div></div>)}</div></section>}
      <div className="actions"><button onClick={exportCase} disabled={!caseTemplate}>Export as eval case</button><span className="muted">profile: {pack.ranking_profile} {pack.degraded ? "- lexical degraded mode" : ""}</span></div>
    </div>}
  </section>;
}

function Settings({ scope }: { scope: Scope }) {
  const settings = useQuery({ queryKey: ["console-settings", scope], queryFn: () => read<any>("/v1/console/settings", scope) });
  if (settings.isLoading) return <Loading />;
  if (settings.error) return <ErrorBox error={settings.error} />;
  const data = settings.data;
  if (!data) return null;
  const weights = data.ranking_profile?.weights || {};
  const scalarWeights = Object.entries(weights).filter(([key, value]) => key !== "trust_weights" && key !== "recency_half_life_days" && typeof value !== "object");
  const trustWeights = Object.entries(weights.trust_weights || {});
  const halfLives = Object.entries(weights.recency_half_life_days || {});
  const advice = data.index_advice || {};
  return <section className="settings-view"><div className="section-head"><div><p className="muted">Project policy is inspectable here; a GitHub credential is accepted once, encrypted, and audit logged.</p><h2>Project settings</h2></div></div><section className="panel"><h3>{data.project.name}</h3><dl className="detail-grid"><div><dt>Project</dt><dd><code>{data.project.slug}</code></dd></div><div><dt>Status</dt><dd>{data.project.status}</dd></div><div><dt>Repository</dt><dd><code>{data.project.repo_url || "Not bound"}</code></dd></div><div><dt>Profile version</dt><dd><code>{data.project.profile_version || "Not recorded"}</code></dd></div></dl></section><GitHubIntegration scope={scope} /><section className="panel"><div className="panel-head"><h3>Ranking profile</h3>{data.ranking_profile && <code>{data.ranking_profile.id}</code>}</div>{data.ranking_profile ? <div className="ranking-profile"><dl className="setting-values">{scalarWeights.map(([name, value]) => <div key={name}><dt>{name.replaceAll("_", " ")}</dt><dd>{String(value)}</dd></div>)}</dl><section className="setting-group"><h4>Trust weights</h4><div className="weight-grid">{trustWeights.map(([tier, value]) => <div key={tier}><TrustMark tier={tier} /><span>{tier}</span><strong>{String(value)}</strong></div>)}</div></section><section className="setting-group"><h4>Recency half-life</h4><div className="weight-grid">{halfLives.map(([type, value]) => <div key={type}><span>{type}</span><strong>{String(value)}d</strong></div>)}</div></section></div> : <p className="muted">No active ranking profile.</p>}</section><section className="panel"><h3>Scope grants</h3>{data.grants.length ? <table><thead><tr><th>Permission</th><th>Reason</th><th>From</th><th>To</th><th>Expires</th></tr></thead><tbody>{data.grants.map((grant: any) => <tr key={grant.id}><td>{grant.permission}</td><td>{grant.reason}</td><td><code>{short(grant.from_id)}</code></td><td><code>{short(grant.to_id)}</code></td><td>{formatDate(grant.expires_at)}</td></tr>)}</tbody></table> : <p className="muted">No active grants touch this project.</p>}</section><section className="panel"><div className="panel-head"><h3>Index capacity</h3><span className={advice.advised ? "status incomplete" : "status passed"}>{advice.advised ? "action required" : "within capacity"}</span></div><dl className="detail-grid"><div><dt>Embedded rows</dt><dd>{advice.rows ?? "-"}</dd></div><div><dt>Threshold</dt><dd>{advice.threshold ?? "-"}</dd></div><div><dt>Index</dt><dd><code>{advice.index || "Not configured"}</code></dd></div><div><dt>Exists</dt><dd>{advice.exists ? "Yes" : "No"}</dd></div></dl>{advice.note && <p className="muted">{advice.note}</p>}{advice.command && <pre>{advice.command}</pre>}</section></section>;
}

function GitHubIntegration({ scope }: { scope: Scope }) {
  const connection = useQuery({ queryKey: ["github-connection", scope], queryFn: () => read<any>("/v1/console/integrations/github", scope) });
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  if (connection.isLoading) return <section className="panel"><h3>GitHub integration</h3><p className="muted">Loading connection status…</p></section>;
  if (connection.error) return <section className="panel"><h3>GitHub integration</h3><ErrorBox error={connection.error} /></section>;
  const data = connection.data;
  if (!data?.github_project) return <section className="panel"><h3>GitHub integration</h3><p className="muted">Bind this project to a GitHub source repository before adding a credential.</p></section>;
  const pat = data.pat;
  async function connect(event: React.FormEvent) {
    event.preventDefault();
    if (!token.trim()) return;
    setBusy(true); setError("");
    try {
      await write("/v1/console/integrations/github/pat", scope, { token }, "PUT");
      setToken("");
      await connection.refetch();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Could not connect GitHub"); }
    finally { setBusy(false); }
  }
  async function disconnect() {
    setBusy(true); setError("");
    try { await remove("/v1/console/integrations/github/pat", scope); await connection.refetch(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Could not disconnect GitHub"); }
    finally { setBusy(false); }
  }
  return <section className="panel github-integration"><div className="panel-head"><div><h3>GitHub integration</h3><p className="muted">GitHub App webhooks stay deployment-managed. A fine-grained PAT is encrypted before storage and is never shown again.</p></div><span className={pat ? "status passed" : "status incomplete"}>{pat ? "PAT connected" : "App only"}</span></div><dl className="detail-grid"><div><dt>Source</dt><dd><code>{data.source_repository || "Not bound"}</code></dd></div><div><dt>Evidence</dt><dd><code>{data.evidence_repository || "Not bound"}</code></dd></div><div><dt>GitHub App</dt><dd>{data.github_app_installed ? "Installed" : "Not configured"}</dd></div>{pat && <><div><dt>Credential</dt><dd><code>{pat.token_hint}</code>{pat.github_login ? ` · ${pat.github_login}` : ""}</dd></div><div><dt>Validated</dt><dd>{formatDate(pat.validated_at)}</dd></div></>}</dl>{!data.webhooks_enabled && <p className="muted">The deployment GitHub integration is disabled, so this PAT cannot be connected until the webhook service is enabled.</p>}{error && <ErrorBox error={new Error(error)} />}{pat ? <div className="actions"><button className="danger" type="button" disabled={busy} onClick={() => void disconnect()}>Disconnect PAT</button><span className="muted">To rotate it, enter a replacement below.</span></div> : null}<form className="github-pat-form" onSubmit={connect}><label>Fine-grained PAT<input aria-label="GitHub fine-grained PAT" type="password" autoComplete="off" spellCheck={false} value={token} onChange={event => setToken(event.target.value)} placeholder="github_pat_…" disabled={busy} /></label><button className="primary" disabled={busy || !token.trim() || !data.webhooks_enabled}>{busy ? "Validating…" : pat ? "Rotate PAT" : "Connect PAT"}</button></form><p className="muted">Grant only Metadata: Read and Contents: Read for the bound repositories. The token is validated against the source repository before saving.</p></section>;
}

function Audit({ scope }: { scope: Scope }) {
  const audit = useQuery({ queryKey: ["audit", scope], queryFn: () => read<{ events: any[] }>("/v1/audit", scope) });
  if (audit.isLoading) return <Loading />;
  if (audit.error) return <ErrorBox error={audit.error} />;
  const events = audit.data?.events || [];
  return <section><div className="section-head"><div><p className="muted">Evidence associated with the current project only.</p><h2>Audit trail</h2></div><span className="status-pill">{events.length} events</span></div>{events.length ? <section className="panel"><table><thead><tr><th>When</th><th>Action</th><th>Actor</th><th>Outcome</th><th>Detail</th></tr></thead><tbody>{events.map(event => <tr key={event.id}><td>{formatDate(event.created_at)}</td><td><code>{event.action}</code></td><td>{event.principal || "system"}</td><td>{event.outcome}</td><td><code>{JSON.stringify(event.detail)}</code></td></tr>)}</tbody></table></section> : <div className="empty">No project audit events are available.</div>}</section>;
}

function Admin({ scope }: { scope: Scope }) {
  const projects = useQuery({ queryKey: ["projects", scope.tenant_id, scope.principal_id], queryFn: () => read<{ projects: any[] }>("/v1/projects", scope) });
  if (projects.isLoading) return <Loading />;
  if (projects.error) return <ErrorBox error={projects.error} />;
  return <section><div className="section-head"><div><p className="muted">Visibility is limited to projects the current principal can read.</p><h2>Projects</h2></div></div><section className="panel"><table><thead><tr><th>Project</th><th>Slug</th><th>Active</th><th>Quarantined</th></tr></thead><tbody>{projects.data?.projects.map(project => <tr key={project.id}><td>{project.name}</td><td><code>{project.slug}</code></td><td>{project.active}</td><td>{project.quarantined}</td></tr>)}</tbody></table></section></section>;
}

function Health({ scope }: { scope: Scope }) {
  const health = useQuery({ queryKey: ["health", scope], queryFn: () => read<any>("/v1/health/project", scope) });
  if (health.isLoading) return <Loading />; if (health.error) return <ErrorBox error={health.error} />;
  return <section><div className="health-score"><span>Project health</span><strong>{health.data.health}<small>/100</small></strong></div><div className="metric-band">{Object.entries(health.data.counts).map(([name, value]) => <div className="metric" key={name}><span>{name.replace("_", " ")}</span><strong>{String(value)}</strong></div>)}</div><section className="panel"><h2>Formula</h2><table><thead><tr><th>Component</th><th>Weight</th><th>Penalty</th><th>Cost</th></tr></thead><tbody>{health.data.formula.map((part: any) => <tr key={part.component}><td>{part.component}</td><td>{part.weight}</td><td>{part.penalty}</td><td>{part.cost}</td></tr>)}</tbody></table></section><section className="panel"><h2>Extraction</h2><p>LLM extraction {health.data.curation.extraction_enabled ? "enabled" : "DISABLED"}: {health.data.curation.reason || "within curation capacity"}</p></section></section>;
}

function Evals({ scope }: { scope: Scope }) {
  const [selected, setSelected] = useState<string | null>(null);
  const runs = useQuery({ queryKey: ["evals", scope], queryFn: () => read<{ runs: any[] }>("/v1/evals", scope) });
  const detail = useQuery({ queryKey: ["eval", scope, selected], queryFn: () => read<any>(`/v1/evals/${selected}`, scope), enabled: Boolean(selected) });
  if (runs.isLoading) return <Loading />;
  if (runs.error) return <ErrorBox error={runs.error} />;
  if (!runs.data) return <Loading label="Loading evaluations" />;
  const evaluationRuns = runs.data.runs;
  const points = [...evaluationRuns].reverse().map((run, index) => ({ x: evaluationRuns.length === 1 ? 500 : 34 + index * (920 / Math.max(evaluationRuns.length - 1, 1)), y: 150 - Math.min(1, Number(run.metrics["recall@5"] || 0)) * 110 }));
  return <section><div className="section-head"><div><p className="muted">Only runs on the same corpus snapshot are comparable.</p><h2>Evaluation history</h2></div></div><section className="panel chart-panel"><h3>Recall@5 by run</h3><svg viewBox="0 0 1000 180" role="img" aria-label="Recall at five trend"><line x1="34" y1="150" x2="966" y2="150" className="timeline-axis" /><line x1="34" y1="51" x2="966" y2="51" className="gate-line" /><text x="40" y="46">gate .90</text><polyline points={points.map(point => `${point.x},${point.y}`).join(" ")} className="trend-line" />{points.map((point, index) => <circle key={evaluationRuns[index].id} cx={point.x} cy={point.y} r="5" className={evaluationRuns[index].status === "passed" ? "point-pass" : "point-fail"} />)}{evaluationRuns.length === 1 && <text className="eval-point-label" x="500" y={points[0].y - 14} textAnchor="middle">single comparable run · recall@5 {evaluationRuns[0].metrics["recall@5"] ?? "-"}</text>}</svg></section><section className="panel"><table><thead><tr><th>Suite</th><th>Status</th><th>Recall@5</th><th>MRR</th><th>Cases</th><th>Completed</th></tr></thead><tbody>{evaluationRuns.map(run => <tr key={run.id} className="clickable" onClick={() => setSelected(run.id)}><td>{run.suite}</td><td><span className={`status ${run.status}`}>{run.status}</span></td><td>{run.metrics["recall@5"] ?? "-"}</td><td>{run.metrics.mrr ?? "-"}</td><td>{run.case_count}</td><td>{formatDate(run.completed_at)}</td></tr>)}</tbody></table></section>{selected && (detail.isLoading ? <Loading label="Loading case evidence" /> : detail.error ? <ErrorBox error={detail.error} /> : detail.data ? <section className="panel"><div className="panel-head"><h2>{detail.data.suite} cases</h2><button className="icon-button" aria-label="Close evaluation detail" title="Close evaluation detail" onClick={() => setSelected(null)}><X size={15} /></button></div><table><thead><tr><th>Case</th><th>Query</th><th>Status</th><th>Recall@5</th></tr></thead><tbody>{detail.data.cases.map((item: any) => <tr key={item.case_id}><td><code>{item.case_id}</code></td><td>{item.query_text}</td><td><span className={`status ${item.status}`}>{item.status}</span></td><td>{item.result["recall@5"] ?? "-"}</td></tr>)}</tbody></table></section> : null)}</section>;
}
