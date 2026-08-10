import type { Scope } from "./providers";

type Json = Record<string, unknown>;

function query(scope: Scope, params: Record<string, string | number | null | undefined> = {}) {
  const search = new URLSearchParams({ tenant_id: scope.tenant_id, project_id: scope.project_id });
  if (scope.principal_id) search.set("principal_id", scope.principal_id);
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "") search.set(key, String(value));
  }
  return search.toString();
}

function auth(scope: Scope) {
  const headers = new Headers();
  if (scope.access_token) headers.set("Authorization", `Bearer ${scope.access_token}`);
  return headers;
}

export async function read<T>(path: string, scope: Scope, params: Record<string, string | number | null | undefined> = {}): Promise<T> {
  const response = await fetch(`${path}?${query(scope, params)}`, { credentials: "same-origin", headers: auth(scope) });
  if (!response.ok) throw new Error((await response.json().catch(() => ({})) as Json).detail as string || `Request failed (${response.status})`);
  return response.json() as Promise<T>;
}

export async function write<T>(path: string, scope: Scope, body: Json, method = "POST"): Promise<T> {
  const headers = auth(scope);
  headers.set("Content-Type", "application/json");
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers,
    body: JSON.stringify({ ...body, tenant_id: scope.tenant_id, project_id: scope.project_id, principal_id: scope.principal_id })
  });
  if (!response.ok) throw new Error((await response.json().catch(() => ({})) as Json).detail as string || `Request failed (${response.status})`);
  return response.json() as Promise<T>;
}

export async function remove<T>(path: string, scope: Scope): Promise<T> {
  const response = await fetch(`${path}?${query(scope)}`, { method: "DELETE", credentials: "same-origin", headers: auth(scope) });
  if (!response.ok) throw new Error((await response.json().catch(() => ({})) as Json).detail as string || `Request failed (${response.status})`);
  return response.json() as Promise<T>;
}
