import type { ReactNode } from "react";

import { cn } from "../../lib/cn";

const WIDTHS = { sm: "max-w-lg", md: "max-w-3xl", lg: "max-w-5xl" } as const;

/**
 * Scrollable page body. The app shell is a fixed-height (h-dvh) column, so each page owns its
 * own scroll container.
 */
export function Page({
  width = "lg",
  className,
  children,
}: {
  width?: keyof typeof WIDTHS;
  className?: string;
  children: ReactNode;
}) {
  return (
    <main className="min-h-0 flex-1 overflow-y-auto">
      <div className={cn("mx-auto w-full space-y-6 p-4 sm:p-6", WIDTHS[width], className)}>
        {children}
      </div>
    </main>
  );
}

export function PageHeader({
  title,
  description,
  actions,
  eyebrow,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  eyebrow?: ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        {eyebrow ? <div className="mb-1 text-xs">{eyebrow}</div> : null}
        <h1 className="font-display text-display-md text-ink">{title}</h1>
        {description ? <div className="mt-1 text-sm text-ink-muted">{description}</div> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  );
}

/** Section heading inside a page. */
export function SectionTitle({ children, className }: { children: ReactNode; className?: string }) {
  return <h2 className={cn("font-display text-display-sm text-ink", className)}>{children}</h2>;
}
