"use client";

import { useEffect, useState } from "react";
import { resolveAgentWsConfig } from "@/lib/agent-ws";

export function useDegradedMode() {
  const [isAgentAvailable, setIsAgentAvailable] = useState(false);

  useEffect(() => {
    let cancelled = false;

    function probe(endpoint: string, onDone: (available: boolean) => void) {
      try {
        const ws = new WebSocket(endpoint);
        const timer = setTimeout(() => {
          ws.close();
          onDone(false);
        }, 4000);

        ws.onopen = () => {
          clearTimeout(timer);
          ws.close();
          onDone(true);
        };
        ws.onerror = () => {
          clearTimeout(timer);
          onDone(false);
        };
      } catch {
        onDone(false);
      }
    }

    async function check() {
      const { healthCheckEndpoints } = await resolveAgentWsConfig();
      const [primary] = healthCheckEndpoints;
      probe(primary, (primaryAvailable) => {
        if (cancelled) return;
        setIsAgentAvailable(primaryAvailable);
      });
    }

    // Delay first probe by 2 s — avoids false "degraded" on cold start when
    // the backend container is still warming up but the WS chat connection
    // (established by assistant-panel) succeeds shortly after.
    const initialTimer = setTimeout(() => {
      if (!cancelled) check();
    }, 2000);
    const interval = setInterval(check, 30_000);
    return () => {
      cancelled = true;
      clearTimeout(initialTimer);
      clearInterval(interval);
    };
  }, []);

  return {
    isAgentAvailable,
    isDegraded: !isAgentAvailable,
  };
}
