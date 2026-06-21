"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  checkForUpdate,
  consumePendingPath,
  consumeSharedIntent,
  installUpdate,
  isNative,
  registerForPush,
  type UpdateInfo,
} from "@/lib/native-bridge";
import { setPendingShare } from "@/lib/mobile-share-store";
import { BiometricGate } from "@/components/mobile/BiometricGate";

const APP_VERSION = process.env.NEXT_PUBLIC_APP_VERSION ?? undefined;

/**
 * Native-shell-only chrome: push registration, app-update check/banner, biometric
 * lock, shared-file intake, push deep-links. The mobile tab bar/drawer is provided
 * by ResizableLayout for all narrow viewports. Renders nothing in a desktop browser.
 *
 * Updates: checks /download/version.json on launch AND whenever the app returns to
 * the foreground. If a newer build exists, shows a banner; "Обновить" downloads the
 * signed APK, verifies sha256, and launches the system installer (one tap — Android
 * always requires user confirmation for sideloaded installs).
 */
export function MobileChrome() {
  const [native, setNative] = useState(false);
  const [update, setUpdate] = useState<UpdateInfo | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const [installing, setInstalling] = useState(false);
  const router = useRouter();

  const runUpdateCheck = useCallback(async () => {
    try {
      const u = await checkForUpdate();
      if (u.available) {
        setUpdate(u);
        setDismissed(false);
      }
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    if (!isNative()) return;
    setNative(true);
    document.documentElement.classList.add("is-native");

    void registerForPush(APP_VERSION);
    void runUpdateCheck();

    // Files shared from other apps (Email/Telegram/MAX…) → confirmation screen.
    void (async () => {
      const shared = await consumeSharedIntent();
      if (shared && shared.files.length) {
        setPendingShare(shared);
        router.push("/mobile/share");
      }
    })();

    // Deep-link path stashed by a push tap during cold start.
    void (async () => {
      const path = await consumePendingPath();
      if (path && path.startsWith("/")) router.push(path);
    })();
  }, [router, runUpdateCheck]);

  // Re-check for updates each time the app comes back to the foreground.
  useEffect(() => {
    if (!native) return;
    function onVisible() {
      if (document.visibilityState === "visible") void runUpdateCheck();
    }
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [native, runUpdateCheck]);

  async function doInstall() {
    setInstalling(true);
    try {
      await installUpdate();
    } finally {
      setInstalling(false);
    }
  }

  if (!native) return null;

  return (
    <>
      <BiometricGate />

      {update && !dismissed && (
        <div className="fixed inset-x-0 top-0 z-[60] bg-sky-600 px-4 py-2 text-sm text-white">
          <div className="flex items-center gap-3">
            <span className="flex-1 font-medium">
              Доступно обновление
              {update.versionName ? ` ${update.versionName}` : ""}
            </span>
            <button
              type="button"
              onClick={doInstall}
              disabled={installing}
              className="rounded bg-white/20 px-3 py-1 font-medium disabled:opacity-70"
            >
              {installing ? "Загрузка…" : "Обновить"}
            </button>
            <button
              type="button"
              onClick={() => setDismissed(true)}
              className="px-2 text-white/80"
              aria-label="Позже"
            >
              ✕
            </button>
          </div>
          {update.changelog && (
            <p className="mt-0.5 line-clamp-2 text-xs text-white/85">
              {update.changelog}
            </p>
          )}
        </div>
      )}
    </>
  );
}
