import { WrenchIcon } from "../components/ui/icons";
import { cn } from "../lib/cn";
import { useMode } from "./ModeContext";

/**
 * Compact Developer-mode switch (wrench icon). Subtle in User mode; amber with a "Dev" label
 * when Developer mode is on. Lives in the app nav only.
 */
export function ModeToggle({ className }: { className?: string }) {
  const { isDev, toggleMode } = useMode();
  return (
    <button
      type="button"
      onClick={toggleMode}
      role="switch"
      aria-checked={isDev}
      aria-label="Developer mode"
      title={isDev ? "Developer mode on — click to turn off" : "Turn on Developer mode"}
      className={cn(
        "inline-flex h-8 items-center gap-1.5 rounded-control border px-2 text-xs font-semibold transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-400",
        isDev
          ? "border-ember-600 bg-ember-950/60 text-ember-200"
          : "border-transparent text-ink-subtle hover:bg-surface-raised hover:text-ink",
        className,
      )}
    >
      <WrenchIcon className="h-4 w-4" />
      {isDev ? <span>Dev</span> : null}
    </button>
  );
}

/** Unmistakable amber DEV badge shown when developer mode is active. */
export function DevBadge({ className }: { className?: string }) {
  const { isDev } = useMode();
  if (!isDev) return null;
  return (
    <span
      className={cn(
        "rounded-md border border-ember-600 bg-ember-900/50 px-1.5 py-0.5 text-[0.65rem] font-bold uppercase tracking-wider text-ember-200",
        className,
      )}
    >
      Dev
    </span>
  );
}
