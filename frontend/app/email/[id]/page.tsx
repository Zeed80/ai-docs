"use client";

import { useParams } from "next/navigation";
import { EmailClient } from "../_components/EmailClient";

export default function EmailThreadPage() {
  const params = useParams<{ id: string }>();
  return <EmailClient initialThreadId={params.id} />;
}
