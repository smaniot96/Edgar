import { cn } from "../lib/cn";
import { useMode } from "./ModeContext";

/**
 * Compact User/Developer switch. Active mode is highlighted; when in Developer
 * mode the control wears an amber tint and a 🔧 icon so the mode is obvious.
 */
export function ModeToggle({ className }: { className?: string }) {
  const { mode, isDev, toggleMode } = useMode();
  return (
    <button
      type="button"
      onClick={toggleMode}
      role="switch"
      aria-checked={isDev}
      aria-label={`Switch to ${isDev ? "User" : "Developer"} mode`}
      title={`Currently in ${isDev ? "Developer" : "User"} mode — click to switch`}
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-1 py-0.5 text-xs font-medium transition-colors",
        isDev
          ? "border-amber-600 bg-amber-950/60 text-amber-200"
          : "border-[#333] bg-[#1a1c20] text-[#9a9a9a]",
        className,
      )}
    >
      <span
        className={cn(
          "rounded-full px-2 py-0.5",
          mode === "user" ? "bg-[#2a2d33] text-[#e6e6e6]" : "text-[#9a9a9a]",
        )}
      >
        User
      </span>
      <span
        className={cn(
          "rounded-full px-2 py-0.5",
          isDev ? "bg-amber-700/70 text-amber-50" : "text-[#9a9a9a]",
        )}
      >
        🔧 Developer
      </span>
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
        "rounded-md border border-amber-600 bg-amber-900/50 px-1.5 py-0.5 text-[0.65rem] font-bold uppercase tracking-wider text-amber-200",
        className,
      )}
    >
      Dev
    </span>
  );
}
