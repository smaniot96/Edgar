import { useEffect, useState } from "react";
import { browserTurnTimelines, subscribeBrowserTurnTimelines, type BrowserTurnStage } from "./browserTurnTimeline";

const STAGES: { key: BrowserTurnStage; label: string }[] = [
  { key: "busy_paint", label: "Busy feedback painted" },
  { key: "headers", label: "Response headers" },
  { key: "first_token", label: "First text received" },
  { key: "first_text_paint", label: "First text paint opportunity" },
  { key: "done", label: "Committed result received" },
  { key: "composer_ready", label: "Composer ready painted" },
  { key: "journal_settled", label: "Journal refreshed" },
  { key: "map_settled", label: "Open map refreshed" },
  { key: "failed", label: "Failed / interrupted" },
];

export function BrowserTurnTimings({ sessionId }: { sessionId: number }) {
  const [, update] = useState(0);
  useEffect(() => subscribeBrowserTurnTimelines(() => update((value) => value + 1)), []);
  const rows = browserTurnTimelines(sessionId).slice(-10).reverse();
  return <details className="m-3 rounded border border-ember-800 bg-canvas p-2 text-xs text-ink-muted">
    <summary className="cursor-pointer font-semibold text-ember-300">Browser turn timings</summary>
    <p className="mt-2">Time from submit, separate from server diagnostics. Request IDs correlate with the turn request. Paint is observed after two animation frames in a visible tab; it is not a hardware display measurement. Headers are not first text. Refresh stages mean that turn’s refresh request settled, including errors; cancelled or superseded refreshes remain unmeasured. Browser observations reset on reload.</p>
    {!rows.length ? <p className="mt-2">No browser turns measured in this page.</p> : rows.map((row) => <div key={`${row.requestId}:${row.attempt}`} className="mt-3 border-t border-line pt-2">
      <p className="break-all font-mono">{row.requestId} · attempt {row.attempt}</p>
      <dl className="mt-1 grid grid-cols-2 gap-x-2 gap-y-1">{STAGES.map(({ key, label }) => <div key={key} className="contents"><dt>{label}</dt><dd>{row.stages[key] === undefined ? "unmeasured" : `${Math.round(row.stages[key]!)} ms`}</dd></div>)}</dl>
    </div>)}
  </details>;
}
