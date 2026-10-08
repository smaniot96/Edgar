import { cn } from "../../lib/cn";

export type ButtonVariant = "primary" | "secondary" | "danger" | "ghost";
export type ButtonSize = "sm" | "md";

const BASE =
  "inline-flex items-center justify-center gap-1.5 rounded-control font-semibold transition-colors " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-400 " +
  "focus-visible:ring-offset-2 focus-visible:ring-offset-canvas " +
  "disabled:cursor-not-allowed disabled:opacity-50";

const VARIANTS: Record<ButtonVariant, string> = {
  primary: "bg-ember-500 text-canvas hover:bg-ember-400 disabled:hover:bg-ember-500",
  secondary:
    "border border-line-strong bg-surface text-ink hover:border-ember-700 hover:bg-surface-raised",
  danger: "border border-danger-strong/80 text-danger hover:bg-danger-soft",
  ghost: "text-ink-muted hover:bg-surface-raised hover:text-ink",
};

const SIZES: Record<ButtonSize, string> = {
  sm: "h-8 px-3 text-xs",
  md: "h-10 px-4 text-sm",
};

/** Class string for button-looking elements (use for <Link>s styled as buttons). */
export function buttonClasses(
  variant: ButtonVariant = "primary",
  size: ButtonSize = "md",
  className?: string,
): string {
  return cn(BASE, VARIANTS[variant], SIZES[size], className);
}

/** Shared control look for <input>, <textarea>, <select>. */
export const controlClasses =
  "w-full rounded-control border border-line-strong bg-surface-sunken px-3 py-2 text-sm text-ink " +
  "placeholder:text-ink-subtle transition-colors " +
  "focus:border-ember-500 focus:outline-none focus:ring-1 focus:ring-ember-500 " +
  "disabled:opacity-50 aria-[invalid=true]:border-danger";

/** Panel/card surface. */
export const cardClasses = "rounded-card border border-line bg-surface";

/** Text link in the ember accent. */
export const linkClasses =
  "text-ember-300 underline-offset-2 hover:text-ember-200 hover:underline " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-400 rounded-sm";
