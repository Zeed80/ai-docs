"use client";

export type ActiveTabularSurface = {
  id: string;
  kind: "spec_table" | "sheet";
  title?: string;
  sheet_id?: string;
  write_policy: "approval" | "scratch";
};

let activeSurface: ActiveTabularSurface | null = null;

export function setActiveTabularSurface(surface: ActiveTabularSurface | null) {
  activeSurface = surface;
  if (typeof window !== "undefined") {
    window.dispatchEvent(
      new CustomEvent("workspace-active-surface-changed", {
        detail: { surface },
      }),
    );
  }
}

export function getActiveTabularSurface(): ActiveTabularSurface | null {
  return activeSurface;
}

export function getActiveWorkspaceContext():
  | { active_tabular_surface: ActiveTabularSurface }
  | undefined {
  if (!activeSurface) return undefined;
  return { active_tabular_surface: activeSurface };
}
