import type { ReactNode } from "react";

export function LoadingState({ what }: { what?: string }) {
  return <p className="empty-state">{what ? `Loading ${what}…` : "Loading…"}</p>;
}

export function ErrorState({ what, children }: { what: string; children: ReactNode }) {
  return (
    <p className="error-state">
      Failed to load {what}: {children}
    </p>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <p className="empty-state">{children}</p>;
}
