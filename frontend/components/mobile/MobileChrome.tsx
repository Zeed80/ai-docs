"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import {
  checkForUpdate,
  consumeSharedIntent,
  installUpdate,
  isNative,
  registerForPush,
  type UpdateInfo,
} from "@/lib/native-bridge";
import { setPendingShare } from "@/lib/mobile-share-store";
import { BottomNav } from "@/components/mobile/BottomNav";
import { BiometricGate } from "@/components/mobile/BiometricGate";

const APP_VERSION = process.env.NEXT_PUBLIC_APP_VERSION ?? undefined;

/**
 * Orchestrates the native shell experience: registers push, checks for app
 * updates, intakes shared files, and renders the mobile chrome (tab bar +
 * biometric lock). Renders nothing extra in a normal browser.
 */
export function MobileChrome() {
  const [native, setNative] = useState(false);
  const [update, setUpdate] = useState<UpdateInfo | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (!isNative()) return;
    setNative(true);
    document.documentElement.classList.add("is-native");

    void registerForPush(APP_VERSION);

    void (async () => {
      const u = await checkForUpdate();
      if (u.available) setUpdate(u);
    })();

    // Files shared from other apps (Email/Telegram/MAX…) → confirmation screen.
    void (async () => {
      const shared = await consumeSharedIntent();
      if (shared && shared.files.length) {
        setPendingShare(shared);
        router.push("/mobile/share");
      }
    })();
  }, [router]);

  if (!native) return null;

  const hideNav = pathname?.startsWith("/auth/");

  return (
    <>
      <BiometricGate />

      {update && !dismissed && (
        <div className="fixed inset-x-0 top-0 z-50 flex items-center gap-3 bg-sky-600 px-4 py-2 text-sm text-white">
          <span className="flex-1">
            Доступно обновление
            {update.versionName ? ` ${update.versionName}` : ""}
          </span>
          <button
            type="button"
            onClick={() => void installUpdate()}
            className="rounded bg-white/20 px-3 py-1 font-medium"
          >
            Обновить
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
      )}

      {!hideNav && <BottomNav />}
    </>
  );
}
