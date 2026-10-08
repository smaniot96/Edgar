import type { ReactNode } from "react";

import { cn } from "../lib/cn";

export type PillTone = "neutral" | "ember" | "success" | "danger" | "info" | "warning";

const TONES: Record<PillTone, string> = {
  neutral: "bg-surface-raised text-ink-muted",
  ember: "bg-ember-950/70 text-ember-300",
  success: "bg-success-soft text-success",
  danger: "bg-danger-soft text-danger",
  info: "bg-info-soft text-info",
  warning: "bg-warning-soft text-warning",
};

export function Pill({
  children,
  className,
  tone = "neutral",
}: {
  children: ReactNode;
  className?: string;
  tone?: PillTone;
}) {
  // A caller-supplied background (e.g. Play's HP colouring) wins over the tone's background.
  const overridesBg = className?.split(/\s+/).some((c) => c.startsWith("bg-")) ?? false;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium",
        !overridesBg && TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}
