import { useEffect } from "react";

import { apiBase } from "../../api/client";
import { Pill } from "../../components/Pill";
import { Button } from "../../components/ui/Button";
import { EmptyState, LoadingText } from "../../components/ui/EmptyState";
import { Input } from "../../components/ui/Field";
import type { JournalCategory, JournalEntry, JournalRead } from "./types";

const CATEGORIES: { value: JournalCategory | "all"; label: string }[] = [
  { value: "all", label: "Everything" },
  { value: "goal", label: "Goals" },
  { value: "milestone", label: "Milestones" },
  { value: "clue", label: "Clues" },
  { value: "hint", label: "Hints" },
  { value: "person", label: "People" },
  { value: "place", label: "Places" },
  { value: "promise", label: "Promises" },
  { value: "clock", label: "Story urgency" },
  { value: "consequence", label: "Story changes" },
];

export interface JournalPanelProps {
  data: JournalRead | null;
  loading: boolean;
  error: unknown;
  stale: boolean;
  onRefresh: () => void;
  selectedEntryId: string | null;
  onSelectedEntryChange: (id: string | null) => void;
  category: JournalCategory | "all";
  onCategoryChange: (value: JournalCategory | "all") => void;
  query: string;
  onQueryChange: (value: string) => void;
  onOpenHistory?: (historyEventId: number) => void;
}

function statusLabel(status: JournalEntry["status"]): string {
  return status.replaceAll("_", " ");
}

function EntryCard({
  id,
  entry,
  selected,
  onSelect,
  onOpenHistory,
  onSelectRelated,
}: {
  id: string;
  entry: JournalEntry;
  selected: boolean;
  onSelect: () => void;
  onSelectRelated: (id: string) => void;
  onOpenHistory?: (historyEventId: number) => void;
}) {
  return (
    <article
      id={id}
      aria-current={selected ? "true" : undefined}
      className={`rounded-control border bg-surface p-3 ${selected ? "border-ember-500 ring-1 ring-ember-500/40" : "border-line"}`}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-xs font-medium uppercase tracking-wide text-ink-subtle">
            {entry.category.replaceAll("_", " ")}
          </p>
          <h3 className="mt-0.5 break-words font-semibold text-ink">{entry.title}</h3>
        </div>
        <div className="flex items-center gap-2">
          <Pill tone={entry.status === "completed" || entry.status === "fulfilled" ? "success" : "neutral"}>
            {statusLabel(entry.status)}
          </Pill>
          <Button
            variant="secondary"
            size="sm"
            aria-pressed={selected}
            onClick={onSelect}
          >
            {selected ? "Selected" : "Select"}
          </Button>
        </div>
      </div>
      {entry.text ? <p className="mt-2 whitespace-pre-wrap break-words [overflow-wrap:anywhere] text-sm leading-relaxed text-ink-muted">{entry.text}</p> : null}
      <dl className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-subtle">
        {entry.origin.turn_number !== null ? (
          <div>
            <dt className="inline">First recorded: </dt>
            <dd className="inline">Turn {entry.origin.turn_number}</dd>
          </div>
        ) : null}
        {entry.origin.place ? (
          <div>
            <dt className="inline">Place: </dt>
            <dd className="inline">{entry.origin.place}</dd>
          </div>
        ) : null}
        {entry.origin.actor ? (
          <div>
            <dt className="inline">Person: </dt>
            <dd className="inline">{entry.related_entry_id ? (
              <button type="button" className="underline underline-offset-2" onClick={() => onSelectRelated(entry.related_entry_id!)}>
                {entry.origin.actor}
              </button>
            ) : entry.origin.actor}</dd>
          </div>
        ) : null}
        {entry.origin.citation ? (
          <div>
            <dt className="inline">Source: </dt>
            <dd className="inline">{entry.origin.citation}</dd>
          </div>
        ) : null}
      </dl>
      {entry.history.length > 0 ? (
        <details className="mt-3 border-t border-line pt-2 text-sm">
          <summary className="cursor-pointer text-xs font-medium text-ink-muted">
            History ({entry.history.length} recorded changes)
          </summary>
          <ol className="mt-2 space-y-1 text-xs text-ink-subtle">
            {entry.history.map((change, index) => (
              <li key={`${change.history_event_id ?? "none"}-${index}`} className="flex items-center justify-between gap-3">
                <span>
                  {change.turn_number !== null ? `Turn ${change.turn_number} · ` : ""}
                  {statusLabel(change.status)}
                </span>
                {change.history_event_id !== null && onOpenHistory ? (
                  <button
                    type="button"
                    className="font-medium text-ember-700 underline decoration-ember-400 underline-offset-2 hover:text-ember-900"
                    onClick={() => onOpenHistory(change.history_event_id!)}
                  >
                    View narration
                  </button>
                ) : null}
              </li>
            ))}
          </ol>
        </details>
      ) : null}
    </article>
  );
}

function ContextCard({ label, entry }: { label: string; entry: JournalEntry | null }) {
  if (!entry) return null;
  return (
    <div className="rounded-control bg-surface-raised px-3 py-2">
      <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-subtle">{label}</p>
      <p className="mt-0.5 truncate text-sm font-medium text-ink" title={entry.title}>{entry.title}</p>
      {entry.text ? <p className="mt-0.5 line-clamp-2 text-xs text-ink-muted">{entry.text}</p> : null}
    </div>
  );
}

/** Presentational journal content; the Play workspace owns its overlay and all state. */
export function JournalPanel({
  data,
  loading,
  error,
  stale,
  onRefresh,
  selectedEntryId,
  onSelectedEntryChange,
  category,
  onCategoryChange,
  query,
  onQueryChange,
  onOpenHistory,
}: JournalPanelProps) {
  const params = new URLSearchParams();
  if (query.trim()) params.set("q", query.trim());
  if (category !== "all") params.set("category", category);
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const exportHref = data ? `${apiBase}/api/sessions/${data.session_id}/journal/export${suffix}` : "#";
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const entries = (data?.entries ?? []).filter((entry) => {
    if (category !== "all" && entry.category !== category) return false;
    if (!normalizedQuery) return true;
    return [
      entry.title,
      entry.text,
      entry.status,
      entry.origin.place,
      entry.origin.actor,
      entry.origin.citation,
    ].filter(Boolean).join(" ").toLocaleLowerCase().includes(normalizedQuery);
  });

  useEffect(() => {
    if (!selectedEntryId || !entries.some((entry) => entry.id === selectedEntryId)) return;
    const frame = requestAnimationFrame(() => {
      document.getElementById(`journal-entry-${selectedEntryId}`)?.scrollIntoView?.({ block: "nearest" });
    });
    return () => cancelAnimationFrame(frame);
  }, [selectedEntryId, entries]);

  return (
    <section aria-labelledby="journal-heading" className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="journal-heading" className="font-serif text-xl font-semibold text-ink">Journal</h2>
          {data ? <p className="mt-1 text-xs text-ink-muted">Session {data.session_id}</p> : null}
        </div>
        {data ? (
          <a
            className="text-sm font-medium text-ember-700 underline decoration-ember-400 underline-offset-2 hover:text-ember-900"
            href={exportHref}
            download
          >
            Export JSON
          </a>
        ) : null}
      </div>

      <div className="grid gap-2 sm:grid-cols-2">
        <ContextCard label="Current place" entry={data?.current_place ?? null} />
        <ContextCard label="Active goal" entry={data?.active_goal ?? null} />
      </div>

      <div className="space-y-3">
        <label className="block">
          <span className="sr-only">Search journal</span>
          <Input
            type="search"
            placeholder="Search this journal"
            value={query}
            onChange={(event) => onQueryChange(event.target.value)}
          />
        </label>
        <div className="flex items-center gap-2">
          <label htmlFor="journal-category" className="sr-only">Filter by category</label>
          <select
            id="journal-category"
            className="min-w-0 flex-1 rounded-control border border-line bg-surface px-3 py-2 text-sm text-ink"
            value={category}
            onChange={(event) => onCategoryChange(event.target.value as JournalCategory | "all")}
          >
            {CATEGORIES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
          <Button variant="secondary" size="sm" loading={loading} onClick={onRefresh}>Refresh</Button>
        </div>
      </div>

      {stale ? (
        <p role="status" className="rounded-control bg-warning-soft px-3 py-2 text-sm text-warning">
          Showing the last saved journal while the latest refresh is pending or unavailable.
        </p>
      ) : null}
      {error ? (
        <div role="alert" className="rounded-control border border-danger-strong/60 bg-danger-soft/60 px-3 py-2 text-sm text-danger">
          <p>{data ? "The journal could not be refreshed. The last saved version is still shown." : "This session journal could not be loaded."}</p>
          {!data ? <Button className="mt-2" variant="secondary" size="sm" onClick={onRefresh}>Retry</Button> : null}
        </div>
      ) : null}
      {loading && !data ? <LoadingText /> : null}
      {data?.tracking === "legacy_untracked" ? (
        <p className="rounded-control bg-info-soft px-3 py-2 text-sm text-info">
          This session predates journal tracking. New committed discoveries will appear here.
        </p>
      ) : null}
      {!loading && data && entries.length === 0 ? (
        <EmptyState title={query || category !== "all" ? "No matching entries" : "Nothing recorded yet"}>
          {query || category !== "all"
            ? "Try a different search or category."
            : "Committed discoveries, promises, places, and story changes will appear here as you play."}
        </EmptyState>
      ) : null}
      <div className="space-y-3">
        {entries.map((entry) => (
          <EntryCard
            key={entry.id}
            id={`journal-entry-${entry.id}`}
            entry={entry}
            selected={selectedEntryId === entry.id}
            onSelect={() => onSelectedEntryChange(selectedEntryId === entry.id ? null : entry.id)}
            onOpenHistory={onOpenHistory}
            onSelectRelated={(id) => {
              onCategoryChange("all");
              onQueryChange("");
              onSelectedEntryChange(id);
            }}
          />
        ))}
      </div>
    </section>
  );
}

export default JournalPanel;
