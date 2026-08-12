"use client";

import { useParams } from "next/navigation";

import CadEditorShell from "@/components/cad/editor/CadEditorShell";

/** Full-screen route — components/ui/client-layout.tsx recognizes
 * /cad/<id>/editor and skips the sidebar/chat chrome for it. */
export default function CadEditorPage() {
  const params = useParams<{ id: string }>();
  const id = params?.id;
  if (!id) return null;
  return <CadEditorShell generationId={id} />;
}
