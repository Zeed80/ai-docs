"use client";

/**
 * Продление сессии в фоне.
 *
 * Токен Authentik живёт час. Повтор после 401 есть только в общих помощниках
 * запросов, а многие экраны, WebSocket чата и встроенные страницы ходят мимо
 * них — для них сессия кончалась внезапно. Поэтому, пока страница открыта,
 * сессия продлевается заранее: каждые 10 минут и при возвращении на вкладку
 * сервер продлевает токен, если тому осталось меньше 20 минут.
 */

import { useEffect } from "react";
import { refreshSession } from "@/lib/auth";

const CHECK_EVERY_MS = 10 * 60 * 1000;
const RENEW_WITHIN_S = 20 * 60;

export function SessionKeeper() {
  useEffect(() => {
    if (window.location.pathname.startsWith("/auth/")) return;
    const tick = () => void refreshSession(RENEW_WITHIN_S);
    tick();
    const id = window.setInterval(tick, CHECK_EVERY_MS);
    const onVisible = () => {
      if (document.visibilityState === "visible") tick();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);
  return null;
}
