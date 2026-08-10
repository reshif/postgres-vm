"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createContext, useContext, useMemo, useState } from "react";

export type Scope = {
  tenant_id: string;
  project_id: string;
  principal_id?: string | null;
  // Never persisted to localStorage. OAuth access tokens live only for the
  // browser tab in sessionStorage and are forwarded to the same-origin API.
  access_token?: string | null;
};

const ScopeContext = createContext<{ scope: Scope | null; setScope: (scope: Scope | null) => void }>({
  scope: null,
  setScope: () => undefined
});
const AsOfContext = createContext<{ asOf: string | null; setAsOf: (value: string | null) => void }>({
  asOf: null,
  setAsOf: () => undefined
});

export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(() => new QueryClient({
    defaultOptions: { queries: { staleTime: 15_000, retry: 1, refetchOnWindowFocus: false } }
  }));
  const [scope, setScope] = useState<Scope | null>(null);
  const [asOf, setAsOf] = useState<string | null>(null);
  const scopeValue = useMemo(() => ({ scope, setScope }), [scope]);
  const asOfValue = useMemo(() => ({ asOf, setAsOf }), [asOf]);
  return <QueryClientProvider client={queryClient}>
    <ScopeContext.Provider value={scopeValue}>
      <AsOfContext.Provider value={asOfValue}>{children}</AsOfContext.Provider>
    </ScopeContext.Provider>
  </QueryClientProvider>;
}

export const useScope = () => useContext(ScopeContext);
export const useAsOf = () => useContext(AsOfContext);
