"use client";

import { useEffect } from "react";
import { useParams, useRouter } from "next/navigation";

// This page redirects to the supplier profile with the catalog tab open.
// It keeps backwards compatibility with any links to /catalogs/[supplierId].
export default function CatalogSupplierRedirect() {
  const params = useParams();
  const router = useRouter();
  const supplierId = params.supplierId as string;

  useEffect(() => {
    if (supplierId) {
      router.replace(`/suppliers/${supplierId}?tab=catalog`);
    }
  }, [supplierId, router]);

  return (
    <div className="flex items-center justify-center h-full text-white/40 text-sm">
      Перенаправление...
    </div>
  );
}
