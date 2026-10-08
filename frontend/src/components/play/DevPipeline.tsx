import { cn } from "../../lib/cn";

const STAGES = ["parsing", "retrieving", "adjudicating", "narrating", "saving"] as const;

/** Dev-mode strip showing the live turn pipeline stage. */
export function DevPipeline({ stage }: { stage: string | null }) {
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-1.5 border-b border-ember-900/40 bg-ember-950/20 px-4 py-1.5 text-[0.7rem]">
      <span className="mr-1 font-semibold uppercase tracking-wide text-ember-300">Pipeline</span>
      {STAGES.map((s) => (
        <span
          key={s}
          className={cn(
            "rounded-full border px-2 py-0.5 font-mono",
            stage === s
              ? "border-ember-500 bg-ember-700/60 text-ember-50"
              : "border-line bg-surface text-ink-subtle",
          )}
        >
          {s}
        </span>
      ))}
    </div>
  );
}
