import { cn } from "../../lib/cn";

function tone(ratio: number | null): string {
  if (ratio == null) return "bg-line-strong";
  if (ratio <= 0) return "bg-line-strong";
  if (ratio <= 0.25) return "bg-danger";
  if (ratio <= 0.5) return "bg-ember-500";
  return "bg-success";
}

/** Labelled hit-point meter. `compact` drops the text label (for dense rows). */
export function HpBar({
  current,
  max,
  className,
  compact = false,
  label = "HP",
}: {
  current: number | null;
  max: number | null;
  className?: string;
  compact?: boolean;
  label?: string;
}) {
  const known = current != null && max != null && max > 0;
  const ratio = known ? Math.max(0, Math.min(1, current / max)) : null;
  const down = known && current <= 0;
  const text = known ? `${current}/${max}` : "—/—";
  return (
    <div className={cn("flex min-w-0 items-center gap-2", className)}>
      {!compact ? (
        <span className="shrink-0 text-[0.7rem] font-semibold uppercase tracking-wider text-ink-muted">
          {label}
        </span>
      ) : null}
      <div
        role="meter"
        aria-label={`${label} ${text}${down ? ", down" : ""}`}
        aria-valuemin={0}
        aria-valuemax={known ? max : undefined}
        aria-valuenow={known ? current : undefined}
        className="h-1.5 min-w-[3rem] flex-1 overflow-hidden rounded-full bg-surface-raised"
      >
        <div
          className={cn("h-full rounded-full transition-[width] duration-500 motion-reduce:transition-none", tone(ratio))}
          style={{ width: `${(ratio ?? 0) * 100}%` }}
        />
      </div>
      <span
        className={cn(
          "shrink-0 font-mono text-xs tabular-nums",
          down ? "font-semibold text-danger" : ratio != null && ratio <= 0.25 ? "text-danger" : "text-ink",
        )}
      >
        {down ? "Down" : text}
      </span>
    </div>
  );
}
