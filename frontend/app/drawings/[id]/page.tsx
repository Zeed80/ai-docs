import { DrawingWorkspace } from "@/components/drawings/DrawingWorkspace";

interface Props {
  params: Promise<{ id: string }>;
}

export default async function DrawingPage({ params }: Props) {
  const { id } = await params;
  return (
    <div className="h-full">
      <DrawingWorkspace drawingId={id} />
    </div>
  );
}
