"use client";

import { useEffect, useState } from "react";

export function useDegradedMode() {
  const [isAgentAvailable, setIsAgentAvailable] = useState(false);

  useEffect(() => {
    let cancelled = false;

    async function check() {
      try {
        const response = await fetch("/health", {cache: "no-store", signal: AbortSignal.timeout(4000)});
        if (!cancelled) setIsAgentAvailable(response.ok);
      } catch {
        if (!cancelled) setIsAgentAvailable(false);
      }
    }

    // Probe the HTTP runtime, not the retired connection-owned chat transport.
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
