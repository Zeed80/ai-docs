"use client";

import { useEffect, useState } from "react";
import {
  clearAgentWsFallback,
  resolveAgentWsConfig,
  setLegacyAgentWsFallback,
} from "@/lib/agent-ws";

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
      const [primary, fallback] = healthCheckEndpoints;
      probe(primary, (primaryAvailable) => {
        if (cancelled) return;
        if (primaryAvailable) {
          clearAgentWsFallback();
          setIsAgentAvailable(true);
          return;
        }
        if (!fallback) {
          setIsAgentAvailable(false);
          return;
        }
        probe(fallback, (fallbackAvailable) => {
          if (cancelled) return;
          if (fallbackAvailable) setLegacyAgentWsFallback();
          setIsAgentAvailable(fallbackAvailable);
        });
      });
    }

    check();
    const interval = setInterval(check, 30_000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  return {
    isAgentAvailable,
    isDegraded: !isAgentAvailable,
  };
}
