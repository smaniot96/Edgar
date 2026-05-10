import type { ReactNode } from "react";

import { cn } from "../lib/cn";

export function Pill({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <span className={cn("rounded-full bg-[#222] px-2 py-0.5 text-xs", className)}>
      {children}
    </span>
  );
}
