"use client";

import { useEffect } from "react";
import { useParams, useRouter } from "next/navigation";

export default function SettingsUserDetailPage() {
  const { sub } = useParams<{ sub: string }>();
  const router = useRouter();
  useEffect(() => {
    router.replace(`/admin/users/${encodeURIComponent(sub)}`);
  }, [sub, router]);
  return null;
}
