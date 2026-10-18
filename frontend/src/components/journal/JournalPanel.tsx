import { useEffect, useMemo, useState } from "react";

import { apiBase } from "../../api/client";
import { downloadAuthorized } from "../../api/resources";
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

const PAGE_SIZE = 24;

/** Keyboard users follow a jump or a revealed batch instead of staying on a control that moved or vanished. */
function focusEntry(id: string) {
  requestAnimationFrame(() => document.getElementById(`journal-entry-${id}`)?.focus({ preventScroll: true }));
}

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
  largeText?: boolean;
}

function statusLabel(status: JournalEntry["status"]): string {
  if (status === "announced") return "announced for later";
  if (status === "known") return "known, not visited";
  return status.replaceAll("_", " ");
}

function ownershipLabel(ownership: NonNullable<JournalEntry["ownership"]>): string {
  switch (ownership) {
    case "carried": return "In this character’s inventory";
    case "left_behind": return "Left at a known place";
    case "with_another_character": return "With another character";
    case "consumed": return "Used up";
    case "untracked": return "Ownership is not recorded";
  }
}

function EntryCard({
  id,
  entry,
  selected,
  onSelect,
  onOpenHistory,
  onSelectRelated,
  largeText,
  relatedEntries,
}: {
  id: string;
  entry: JournalEntry;
  selected: boolean;
  onSelect: () => void;
  onSelectRelated: (id: string) => void;
  onOpenHistory?: (historyEventId: number) => void;
  largeText: boolean;
  relatedEntries: JournalEntry[];
}) {
  return (
    <article
      id={id}
      aria-current={selected ? "true" : undefined}
      tabIndex={-1}
      className={`rounded-control border bg-surface p-3 outline-none focus-visible:ring-2 focus-visible:ring-ember-400 ${selected ? "border-ember-500 ring-1 ring-ember-500/40" : "border-line"}`}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-xs font-medium uppercase tracking-wide text-ink-subtle">
            {entry.category.replaceAll("_", " ")}
          </p>
          <h3 className="mt-0.5 break-words font-semibold text-ink">{entry.category === "person" ? `NPC · ${entry.title}` : entry.title}</h3>
        </div>
        <div className="flex items-center gap-2">
          <Pill tone={entry.status === "completed" || entry.status === "fulfilled" ? "success" : "neutral"}>
            {statusLabel(entry.status)}
          </Pill>
          <Button
            variant="secondary"
            size="sm"
            aria-pressed={selected}
            aria-label={`${selected ? "Selected" : "Select"} ${entry.title}`}
            className="min-h-11"
            onClick={onSelect}
          >
            {selected ? "Selected" : "Select"}
          </Button>
        </div>
      </div>
      {entry.category === "person" && entry.actor_aliases?.length ? (
        <p className="mt-2 text-xs text-ink-subtle"><span className="font-medium">Earned names:</span> {entry.actor_aliases.join(", ")}</p>
      ) : null}
      {entry.text ? <p className={`journal-entry-copy mt-2 select-text whitespace-pre-wrap break-words [overflow-wrap:anywhere] text-ink-muted ${largeText ? "text-base leading-7" : "text-sm leading-relaxed"}`}>{entry.text}</p> : null}
      {entry.ownership ? (
        <p className="mt-2 text-xs text-ink-subtle"><span className="font-medium">Item:</span> {ownershipLabel(entry.ownership)}</p>
      ) : null}
      {entry.category === "clock" && (entry.quantity !== null || entry.clock_maximum != null) ? (
        <p className="mt-2 text-xs text-ink-subtle">
          {entry.quantity !== null ? (
            <>
              <span className="font-medium">Current progress:</span>{" "}
              {entry.clock_maximum !== null && entry.clock_maximum !== undefined
                ? `${entry.quantity} of ${entry.clock_maximum}`
                : entry.quantity}
            </>
          ) : (
            <>
              <span className="font-medium">Known maximum:</span>{" "}{entry.clock_maximum}
            </>
          )}
        </p>
      ) : null}
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
              <button type="button" aria-label={`Open journal entry for ${entry.origin.actor}`} className="min-h-11 max-w-full whitespace-normal break-words text-left underline underline-offset-2" onClick={() => onSelectRelated(entry.related_actor_id ?? entry.related_entry_id!)}>
                NPC · {entry.origin.actor}
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
      {relatedEntries.length > 0 ? (
        <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-xs text-ink-subtle">
          <span className="font-medium">Related:</span>
          {relatedEntries.map((related) => (
            <button key={related.id} type="button" className="min-h-11 max-w-full whitespace-normal break-words text-left text-ember-300 underline decoration-ember-400 underline-offset-2 [overflow-wrap:anywhere]" onClick={() => onSelectRelated(related.id)}>
              {related.category === "place" ? "Place: " : related.category === "clue" ? "Clue: " : "Entry: "}{related.title}
            </button>
          ))}
        </div>
      ) : null}
      {entry.history.length > 0 ? (
        <details className="mt-3 border-t border-line pt-2 text-sm">
          <summary className="min-h-11 cursor-pointer py-3 text-xs font-medium text-ink-muted">
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
                    aria-label={`View narration: ${entry.title}, ${statusLabel(change.status)}`}
                    className="min-h-11 px-2 font-medium text-ember-300 underline decoration-ember-400 underline-offset-2 hover:text-ember-200"
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
      <p className="mt-0.5 whitespace-normal break-words text-sm font-medium text-ink [overflow-wrap:anywhere]">{entry.title}</p>
      {entry.text ? <p className="mt-0.5 line-clamp-2 break-words text-xs text-ink-muted [overflow-wrap:anywhere]">{entry.text}</p> : null}
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
  largeText = false,
}: JournalPanelProps) {
  const params = new URLSearchParams();
  if (query.trim()) params.set("q", query.trim());
  if (category !== "all") params.set("category", category);
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const exportUrl = data ? `${apiBase}/api/sessions/${data.session_id}/journal/export${suffix}` : "";
  const [exportFailed, setExportFailed] = useState(false);
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const entries = useMemo(() => (data?.entries ?? []).filter((entry) => {
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
  }), [data, category, normalizedQuery]);
  const filterKey = `${data?.session_id ?? "none"}:${category}:${normalizedQuery}`;
  const [pageState, setPageState] = useState<{ key: string; count: number }>({ key: "", count: PAGE_SIZE });
  // A new session or filter starts at the beginning. The server payload is already the
  // authorized projection; this only keeps a long visible journal quick to scan.
  const baseVisibleCount = pageState.key === filterKey ? pageState.count : PAGE_SIZE;
  const selectedIndex = selectedEntryId
    ? entries.findIndex((entry) => entry.id === selectedEntryId)
    : -1;
  const visibleCount = Math.max(baseVisibleCount, selectedIndex + 1);
  const visibleEntries = entries.slice(0, visibleCount);

  useEffect(() => {
    if (!selectedEntryId || !entries.some((entry) => entry.id === selectedEntryId)) return;
    const frame = requestAnimationFrame(() => {
      document.getElementById(`journal-entry-${selectedEntryId}`)?.scrollIntoView?.({ block: "nearest" });
    });
    return () => cancelAnimationFrame(frame);
  }, [selectedEntryId, entries]);

  return (
    <section aria-labelledby="journal-heading" className="space-y-4 [overflow-wrap:anywhere] [&_button]:min-h-11">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="journal-heading" className="font-serif text-xl font-semibold text-ink">Journal</h2>
          {data ? <p className="mt-1 text-xs text-ink-muted">Session {data.session_id}</p> : null}
        </div>
        {data ? (
          <div className="flex flex-col items-end gap-1">
            <button
              type="button"
              className="text-sm font-medium text-ember-300 underline decoration-ember-400 underline-offset-2 hover:text-ember-200"
              onClick={() => {
                setExportFailed(false);
                void downloadAuthorized(exportUrl, `journal-session-${data.session_id}.json`).catch(() => setExportFailed(true));
              }}
            >
              Export JSON
            </button>
            {exportFailed ? <p role="status" className="text-xs text-danger">The export could not be downloaded. Try again.</p> : null}
          </div>
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
            className="min-h-11"
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
            className="min-h-11 min-w-0 flex-1 rounded-control border border-line bg-surface px-3 py-2 text-sm text-ink"
            value={category}
            onChange={(event) => onCategoryChange(event.target.value as JournalCategory | "all")}
          >
            {CATEGORIES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
          <Button variant="secondary" size="sm" className="min-h-11" loading={loading} onClick={onRefresh}>Refresh</Button>
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
          {!data ? <Button className="mt-2 min-h-11" variant="secondary" size="sm" onClick={onRefresh}>Retry</Button> : null}
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
        {visibleEntries.map((entry) => (
          <EntryCard
            key={entry.id}
            id={`journal-entry-${entry.id}`}
            entry={entry}
            selected={selectedEntryId === entry.id}
            onSelect={() => onSelectedEntryChange(selectedEntryId === entry.id ? null : entry.id)}
            onOpenHistory={onOpenHistory}
            largeText={largeText}
            relatedEntries={(data?.entries ?? []).filter((related) => entry.related_entry_ids?.includes(related.id))}
            onSelectRelated={(id) => {
              onCategoryChange("all");
              onQueryChange("");
              onSelectedEntryChange(id);
              focusEntry(id);
            }}
          />
        ))}
      </div>
      {entries.length > visibleCount ? (
        <Button
          className="w-full"
          variant="secondary"
          onClick={() => {
            setPageState((current) => ({
              key: filterKey,
              count: Math.min((current.key === filterKey ? current.count : PAGE_SIZE) + PAGE_SIZE, entries.length),
            }));
            focusEntry(entries[visibleCount].id);
          }}
        >
          Show more entries
        </Button>
      ) : null}
    </section>
  );
}

export default JournalPanel;
