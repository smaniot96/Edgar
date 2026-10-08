import type { ReactNode } from "react";

import { cn } from "../../lib/cn";

export function EmptyState({
  title,
  children,
  action,
  className,
}: {
  title: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "rounded-card border border-dashed border-line bg-surface-sunken p-6 text-center",
        className,
      )}
    >
      <p className="text-sm font-medium text-ink">{title}</p>
      {children ? <div className="mt-1 text-sm text-ink-muted">{children}</div> : null}
      {action ? <div className="mt-4 flex justify-center">{action}</div> : null}
    </div>
  );
}

export function LoadingText({ children = "Loading…", className }: { children?: ReactNode; className?: string }) {
  return (
    <p role="status" className={cn("text-sm text-ink-muted", className)}>
      {children}
    </p>
  );
}
