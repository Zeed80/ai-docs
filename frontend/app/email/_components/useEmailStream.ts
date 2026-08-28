import { useEffect, useRef } from "react";
import { getWebSocketBaseUrl } from "@/lib/api-base";

type EmailEvent =
  | { type: "email.new"; mailbox: string; thread_id: string | null; subject?: string; from?: string }
  | { type: "email.sent"; mailbox: string | null }
  | { type: "email.thread_updated"; thread_id: string; mailbox: string };

/**
 * Live updates for the mail client. Reuses the shared notifications WS
 * (/api/notifications/ws) that /inbox already uses; we just filter for the
 * email.* events the IMAP poller / send path publish onto the chat bus.
 */
export function useEmailStream(onEvent: (e: EmailEvent) => void) {
  const cbRef = useRef(onEvent);
  cbRef.current = onEvent;

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let retry: ReturnType<typeof setTimeout> | null = null;

    function connect() {
      if (closed) return;
      try {
        ws = new WebSocket(`${getWebSocketBaseUrl()}/api/notifications/ws`);
      } catch {
        return;
      }
      ws.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data);
          if (typeof data?.type === "string" && data.type.startsWith("email.")) {
            cbRef.current(data as EmailEvent);
          }
        } catch {
          /* ignore */
        }
      };
      ws.onclose = () => {
        if (!closed) retry = setTimeout(connect, 5000);
      };
      ws.onerror = () => ws?.close();
    }

    connect();
    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      ws?.close();
    };
  }, []);
}
