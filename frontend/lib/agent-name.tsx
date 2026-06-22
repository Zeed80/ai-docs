"use client";

// Single source of truth for the assistant's display name across the UI.
// The name lives in the agent settings (BuiltinAgentConfig.agent_name) and
// defaults to «Света». It is fetched once for the app shell and updated live
// whenever settings broadcast an `agent-name:changed` event.

import { createContext, useContext, useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";

export const DEFAULT_AGENT_NAME = "Света";
const LS_KEY = "agent.name";
export const AGENT_NAME_EVENT = "agent-name:changed";

const AgentNameContext = createContext<string>(DEFAULT_AGENT_NAME);

function readCached(): string {
  if (typeof window === "undefined") return DEFAULT_AGENT_NAME;
  return window.localStorage.getItem(LS_KEY) || DEFAULT_AGENT_NAME;
}

async function fetchAgentName(): Promise<string | null> {
  try {
    const r = await fetch(`${getApiBaseUrl()}/api/ai/agent-config`, {
      credentials: "include",
    });
    if (!r.ok) return null;
    const data = (await r.json()) as { agent_name?: string | null };
    const name = (data?.agent_name ?? "").trim();
    return name || DEFAULT_AGENT_NAME;
  } catch {
    return null;
  }
}

export function AgentNameProvider({ children }: { children: React.ReactNode }) {
  const [name, setName] = useState<string>(readCached);

  useEffect(() => {
    let active = true;
    const apply = (next: string) => {
      if (!active) return;
      setName(next);
      try {
        window.localStorage.setItem(LS_KEY, next);
      } catch {
        /* ignore quota errors */
      }
    };
    const load = async () => {
      const next = await fetchAgentName();
      if (next) apply(next);
    };
    void load();

    // Settings broadcasts this after saving / approving the agent name. A
    // string detail updates instantly; otherwise we re-fetch from the server.
    const onChanged = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (typeof detail === "string" && detail.trim()) apply(detail.trim());
      else void load();
    };
    window.addEventListener(AGENT_NAME_EVENT, onChanged);
    return () => {
      active = false;
      window.removeEventListener(AGENT_NAME_EVENT, onChanged);
    };
  }, []);

  return (
    <AgentNameContext.Provider value={name}>
      {children}
    </AgentNameContext.Provider>
  );
}

// Returns the configured assistant name (defaults to «Света»).
export function useAgentName(): string {
  return useContext(AgentNameContext);
}

// Imperative broadcast helper for non-context callers (e.g. settings save).
export function broadcastAgentName(name?: string): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(AGENT_NAME_EVENT, { detail: name }));
}
