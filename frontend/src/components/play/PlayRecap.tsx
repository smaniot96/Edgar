import type { JournalEntry, JournalRead, JournalStatus } from "../journal/types";
import { Button } from "../ui/Button";

interface RecapChange {
  entry: JournalEntry;
  status: JournalStatus;
  turnNumber: number | null;
}

function recentChanges(data: JournalRead): RecapChange[] {
  const changes: RecapChange[] = [];
  for (const entry of data.entries) {
    for (const change of entry.history) {
      changes.push({ entry, status: change.status, turnNumber: change.turn_number });
    }
    if (entry.history.length === 0 && entry.origin.turn_number !== null) {
      changes.push({ entry, status: entry.status, turnNumber: entry.origin.turn_number });
    }
  }
  return changes
    .sort((left, right) => (right.turnNumber ?? -1) - (left.turnNumber ?? -1))
    .slice(0, 3);
}

function isOpenPromise(entry: JournalEntry): boolean {
  return entry.category === "promise" && !["fulfilled", "broken", "failed"].includes(entry.status);
}

export function PlayRecap({
  data,
  loading,
  error,
  onOpenEntry,
  onResume,
}: {
  data: JournalRead | null;
  loading: boolean;
  error: unknown;
  onOpenEntry: (entryId: string) => void;
  onResume: () => void;
}) {
  const changes = data ? recentChanges(data) : [];
  const promises = data?.entries.filter(isOpenPromise) ?? [];

  return (
    <details className="mx-3 mb-2 shrink-0 rounded-control border border-line bg-surface/60 px-3 py-2 sm:mx-4">
      <summary className="cursor-pointer text-sm font-semibold text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-ember-400">
        Session recap
        {data ? <span className="ml-2 font-normal text-ink-muted">{data.entries.length} saved {data.entries.length === 1 ? "entry" : "entries"}</span> : null}
      </summary>
      <div className="mt-3 max-h-[min(35vh,20rem)] space-y-3 overflow-y-auto border-t border-line pt-3 text-sm">
        {error && !data ? <p role="status" className="text-ink-muted">The recap could not be loaded. The saved adventure log is still available.</p> : null}
        {loading && !data ? <p role="status" className="text-ink-muted">Loading saved recap…</p> : null}
        {data?.tracking === "legacy_untracked" ? (
          <p className="text-ink-muted">This session predates journal tracking. Its saved adventure log is available below; new committed discoveries will appear in the Journal.</p>
        ) : null}
        {data && data.tracking !== "legacy_untracked" ? (
          <>
            <dl className="grid gap-2 sm:grid-cols-2">
              <div>
                <dt className="text-xs font-medium uppercase tracking-wide text-ink-subtle">Known place</dt>
                <dd className="mt-0.5">
                  {data.current_place ? <button type="button" className="text-left font-medium text-ember-700 underline decoration-ember-400 underline-offset-2" onClick={() => onOpenEntry(data.current_place!.id)}>{data.current_place.title}</button> : <span className="text-ink-muted">No place recorded</span>}
                </dd>
              </div>
              <div>
                <dt className="text-xs font-medium uppercase tracking-wide text-ink-subtle">Active goal</dt>
                <dd className="mt-0.5">
                  {data.active_goal ? <button type="button" className="text-left font-medium text-ember-700 underline decoration-ember-400 underline-offset-2" onClick={() => onOpenEntry(data.active_goal!.id)}>{data.active_goal.title}</button> : <span className="text-ink-muted">No active goal recorded</span>}
                </dd>
              </div>
            </dl>
            <div className="grid gap-3 sm:grid-cols-2">
              <section aria-label="Recent committed developments">
                <h3 className="text-xs font-medium uppercase tracking-wide text-ink-subtle">Recent developments</h3>
                {changes.length ? (
                  <ul className="mt-1 space-y-1">
                    {changes.map((change, index) => <li key={`${change.entry.id}-${change.status}-${change.turnNumber ?? "unknown"}-${index}`}>
                      <button type="button" className="text-left text-ink hover:text-ember-700" onClick={() => onOpenEntry(change.entry.id)}>
                        {change.turnNumber !== null ? `Turn ${change.turnNumber} · ` : ""}{change.entry.title} · {change.status.replaceAll("_", " ")}
                      </button>
                    </li>)}
                  </ul>
                ) : <p className="mt-1 text-ink-muted">No developments have been recorded yet.</p>}
              </section>
              <section aria-label="Open promises">
                <h3 className="text-xs font-medium uppercase tracking-wide text-ink-subtle">Open promises</h3>
                {promises.length ? (
                  <ul className="mt-1 space-y-1">
                    {promises.slice(0, 3).map((entry) => <li key={entry.id}>
                      <button type="button" className="text-left text-ink hover:text-ember-700" onClick={() => onOpenEntry(entry.id)}>
                        <span className="font-medium">{entry.origin.actor ? `NPC · ${entry.origin.actor}: ` : ""}{entry.title}</span>
                        {entry.text ? <span className="mt-0.5 block line-clamp-2 text-xs text-ink-muted">{entry.text}</span> : null}
                      </button>
                    </li>)}
                  </ul>
                ) : <p className="mt-1 text-ink-muted">No open promises recorded.</p>}
              </section>
            </div>
          </>
        ) : null}
        <Button variant="secondary" size="sm" onClick={onResume}>Resume adventure</Button>
      </div>
    </details>
  );
}
