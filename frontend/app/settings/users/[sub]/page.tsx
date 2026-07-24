"use client";

import { useEffect } from "react";
import { useParams, useRouter } from "next/navigation";

export default function SettingsUserDetailPage() {
  const { sub } = useParams<{ sub: string }>();
  const router = useRouter();
  useEffect(() => {
    // useParams() may hand back an already percent-encoded segment (subs contain
    // ":"). Decode before re-encoding so we don't build a double-encoded URL.
    let decoded = sub;
    try {
      decoded = decodeURIComponent(sub);
    } catch {
      /* keep raw */
    }
    router.replace(`/admin/users/${encodeURIComponent(decoded)}`);
  }, [sub, router]);
  return null;
}
