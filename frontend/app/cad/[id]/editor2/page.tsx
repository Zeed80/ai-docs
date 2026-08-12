"use client";

import { useParams } from "next/navigation";

import CadEditorShell2 from "@/components/cad/editor2/CadEditorShell2";

/** Фаза 0 нового плана — временный маршрут разработки нового редактора,
 * рядом со старым /cad/<id>/editor (не заменяет его, пока не достигнут
 * паритет — см. /root/.claude/plans/starry-mapping-hippo.md, Фаза 5).
 * client-layout.tsx's CHROMELESS_ROUTE распознаёт и этот путь тоже. */
export default function CadEditor2Page() {
  const params = useParams<{ id: string }>();
  const id = params?.id;
  if (!id) return null;
  return <CadEditorShell2 generationId={id} />;
}
