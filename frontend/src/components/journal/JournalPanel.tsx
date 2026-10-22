import { Fragment, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import { apiBase } from "../../api/client";
import { ApiDownloadLink } from "../ui/ApiDownloadLink";
import { Pill } from "../../components/Pill";
import { Button } from "../../components/ui/Button";
import { EmptyState, LoadingText } from "../../components/ui/EmptyState";
import { Input } from "../../components/ui/Field";
import { TabPanel, Tabs } from "../../components/ui/Tabs";
import {
  CATEGORY_LABELS,
  JOURNAL_VIEWS,
  VIEW_LABELS,
  entriesForView,
  entryDomId,
  indexJournal,
  lastRecordedTurn,
  matchesQuery,
  statusLabel,
  statusTone,
  viewFor,
  type JournalIndex,
  type JournalView,
} from "./model";
import type { JournalEntry, JournalRead } from "./types";

const PAGE_SIZE = 24;
const ID_BASE = "journal";

/** Keyboard users follow a jump or a revealed batch instead of staying on a control that moved or vanished. */
function focusEntry(id: string) {
  requestAnimationFrame(() => document.getElementById(entryDomId(id))?.focus({ preventScroll: false }));
}

export interface JournalPanelProps {
  data: JournalRead | null;
  loading: boolean;
  error: unknown;
  stale: boolean;
  onRefresh: () => void;
  selectedEntryId: string | null;
  onSelectedEntryChange: (id: string | null) => void;
  view: JournalView;
  onViewChange: (value: JournalView) => void;
  query: string;
  onQueryChange: (value: string) => void;
  onOpenHistory?: (historyEventId: number) => void;
  largeText?: boolean;
}

function ownershipLabel(ownership: NonNullable<JournalEntry["ownership"]>): string {
  switch (ownership) {
    case "carried": return "You are carrying it.";
    case "left_behind": return "You left it at a known place.";
    case "with_another_character": return "Another character has it.";
    case "consumed": return "It has been used up.";
    case "untracked": return "Who holds it is not recorded.";
  }
}

interface Navigation {
  index: JournalIndex;
  currentPlaceId: string | null;
  openEntry: (id: string) => void;
  onOpenHistory?: (historyEventId: number) => void;
  largeText: boolean;
  selectedEntryId: string | null;
}

/** An inline link to another earned entry; unknown or merged links render as plain text. */
function EntryLink({ id, nav, children }: { id: string | null | undefined; nav: Navigation; children: ReactNode }) {
  const target = id ? nav.index.byId.get(id) : undefined;
  if (!target) return <>{children}</>;
  return (
    <button
      type="button"
      className="inline min-h-0 rounded-sm text-left text-ember-300 underline decoration-ember-400/60 underline-offset-2 hover:text-ember-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-ember-400"
      aria-label={`Open ${CATEGORY_LABELS[target.category].toLowerCase()}: ${target.title}`}
      onClick={() => nav.openEntry(target.id)}
    >
      {children}
    </button>
  );
}

/** "Turn 4 at the towpath": when and where something entered the journal, as one readable line. */
function Provenance({ entry, nav, verb }: { entry: JournalEntry; nav: Navigation; verb: string }) {
  const { turn_number: turn, place, place_id: placeId, citation, history_event_id: eventId } = entry.origin;
  const showPlace = place && placeId !== entry.id;
  if (turn === null && !showPlace && !citation) return null;
  return (
    <p className="mt-1 text-xs text-ink-subtle">
      {verb}
      {turn !== null ? <> on turn {turn}</> : null}
      {showPlace ? <> at <EntryLink id={placeId} nav={nav}>{place}</EntryLink></> : null}
      {citation ? <>, from the book ({citation.replace("Pages", "pp.").replace("Page", "p.")})</> : null}
      .
      {eventId !== null && nav.onOpenHistory ? (
        <>
          {" "}
          <button
            type="button"
            aria-label={`Read the moment: ${entry.title}`}
            className="inline min-h-0 rounded-sm font-medium text-ember-300 underline decoration-ember-400/60 underline-offset-2 hover:text-ember-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-ember-400"
            onClick={() => nav.onOpenHistory?.(eventId)}
          >
            Read the moment
          </button>
        </>
      ) : null}
    </p>
  );
}

function Quote({ text, nav, label }: { text: string | null; nav: Navigation; label?: string }) {
  if (!text) return null;
  return (
    <figure className="mt-2">
      {label ? <figcaption className="text-xs text-ink-subtle">{label}</figcaption> : null}
      <blockquote className={`journal-entry-copy mt-1 select-text whitespace-pre-wrap break-words border-l-2 border-line-strong pl-3 font-serif text-ink-muted [overflow-wrap:anywhere] ${nav.largeText ? "text-base leading-8" : "text-sm leading-7"}`}>
        {text}
      </blockquote>
    </figure>
  );
}

/** Earned answers as a short list: what the player was actually told. */
function Notes({ notes, nav, heading, withIds = false }: { notes: JournalEntry[]; nav: Navigation; heading: string; withIds?: boolean }) {
  if (!notes.length) return null;
  return (
    <section className="mt-3" aria-label={heading}>
      <h4 className="text-xs font-medium text-ink-subtle">{heading}</h4>
      <ul className="mt-1 space-y-2">
        {[...notes].sort((left, right) => (left.origin.turn_number ?? 0) - (right.origin.turn_number ?? 0)).map((note) => (
          <li
            key={note.id}
            id={withIds ? entryDomId(note.id) : undefined}
            tabIndex={withIds ? -1 : undefined}
            aria-current={withIds && nav.selectedEntryId === note.id ? "true" : undefined}
            className="rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-ember-400"
          >
            <p className={`break-words text-ink ${nav.largeText ? "text-base leading-7" : "text-sm leading-6"}`}>{note.text}</p>
            <Provenance entry={note} nav={nav} verb="Learned" />
          </li>
        ))}
      </ul>
    </section>
  );
}

function History({ entry, nav }: { entry: JournalEntry; nav: Navigation }) {
  if (entry.history.length < 2) return null;
  return (
    <details className="mt-2 text-sm">
      <summary className="min-h-11 cursor-pointer py-3 text-xs font-medium text-ink-muted focus-visible:outline focus-visible:outline-2 focus-visible:outline-ember-400">
        How this changed ({entry.history.length} steps)
      </summary>
      <ol className="space-y-1 text-xs text-ink-subtle">
        {entry.history.map((change, index) => (
          <li key={`${change.history_event_id ?? "none"}-${index}`} className="flex items-center justify-between gap-3">
            <span>{change.turn_number !== null ? `Turn ${change.turn_number}: ` : ""}{statusLabel(change.status)}</span>
            {change.history_event_id !== null && nav.onOpenHistory ? (
              <button
                type="button"
                aria-label={`Read the moment: ${entry.title}, ${statusLabel(change.status)}`}
                className="min-h-11 px-2 font-medium text-ember-300 underline decoration-ember-400/60 underline-offset-2 hover:text-ember-200"
                onClick={() => nav.onOpenHistory?.(change.history_event_id!)}
              >
                Read
              </button>
            ) : null}
          </li>
        ))}
      </ol>
    </details>
  );
}

function verbFor(entry: JournalEntry): string {
  switch (entry.category) {
    case "person": return "Met";
    case "place": return entry.status === "visited" ? "First reached" : "Heard of";
    case "clue":
    case "hint": return "Found";
    case "note": return "Learned";
    case "promise": return "Promised";
    default: return "Recorded";
  }
}

function EntryBody({ entry, nav, embeddedNotes }: { entry: JournalEntry; nav: Navigation; embeddedNotes: boolean }) {
  const { index } = nav;
  switch (entry.category) {
    case "person": {
      const promises = index.promisesTo(entry.id);
      return (
        <>
          {entry.actor_aliases && entry.actor_aliases.length > 1 ? (
            <p className="mt-1 text-xs text-ink-subtle">Also known as {entry.actor_aliases.filter((alias) => alias !== entry.title).join(", ")}.</p>
          ) : null}
          <Notes notes={index.notesAbout(entry.id)} nav={nav} heading="What you know about them" />
          {promises.length ? (
            <p className="mt-2 text-sm text-ink-muted">
              {promises.length === 1 ? "You made them a promise: " : "Promises to them: "}
              {promises.map((promise, position) => (
                <Fragment key={promise.id}>{position ? ", " : null}<EntryLink id={promise.id} nav={nav}>{promise.title}</EntryLink></Fragment>
              ))}
            </p>
          ) : null}
          {entry.text ? (
            <details className="mt-2">
              <summary className="min-h-11 cursor-pointer py-3 text-xs font-medium text-ink-muted focus-visible:outline focus-visible:outline-2 focus-visible:outline-ember-400">Where you met</summary>
              <Quote text={entry.text} nav={nav} />
            </details>
          ) : null}
        </>
      );
    }
    case "place": {
      const people = index.peopleMetAt(entry.id);
      return (
        <>
          <Quote text={entry.text} nav={nav} label="Recorded on arrival; people and conditions may have changed." />
          {people.length ? (
            <p className="mt-2 text-sm text-ink-muted">
              Met here:{" "}
              {people.map((person, position) => (
                <Fragment key={person.id}>{position ? ", " : null}<EntryLink id={person.id} nav={nav}>{person.title}</EntryLink></Fragment>
              ))}
            </p>
          ) : null}
          <Notes notes={index.notesAbout(entry.id)} nav={nav} heading="Learned here" />
        </>
      );
    }
    case "clue":
    case "hint": {
      const learned = index.notesAbout(entry.id).filter((note) => index.clueForNote(note.id)?.id === entry.id);
      return (
        <>
          {embeddedNotes ? <Notes notes={learned} nav={nav} heading="What it told you" withIds /> : null}
          <Quote text={entry.text} nav={nav} label={learned.length && embeddedNotes ? "What you saw" : undefined} />
          {entry.ownership ? <p className="mt-2 text-xs text-ink-subtle">{ownershipLabel(entry.ownership)}</p> : null}
        </>
      );
    }
    case "note": {
      const about = (entry.related_entry_ids ?? [])
        .map((id) => index.byId.get(id))
        .filter((related): related is JournalEntry => related?.category === "person");
      return (
        <>
          {about.length ? (
            <p className="mt-1 text-xs text-ink-subtle">
              About{" "}
              {about.map((person, position) => (
                <Fragment key={person.id}>{position ? ", " : null}<EntryLink id={person.id} nav={nav}>{person.title}</EntryLink></Fragment>
              ))}
            </p>
          ) : null}
        </>
      );
    }
    case "clock": {
      const value = entry.quantity;
      const maximum = entry.clock_maximum ?? null;
      return (
        <>
          <Quote text={entry.text} nav={nav} />
          {value !== null && maximum !== null ? (
            <div className="mt-2 flex items-center gap-2 text-xs text-ink-subtle">
              <meter min={0} max={maximum} value={value} aria-label={`${entry.title} progress`} className="h-2 w-32" />
              <span>{value} of {maximum}</span>
            </div>
          ) : value !== null ? (
            <p className="mt-2 text-xs text-ink-subtle">Current progress: {value}</p>
          ) : maximum !== null ? (
            <p className="mt-2 text-xs text-ink-subtle">Known maximum: {maximum}</p>
          ) : null}
        </>
      );
    }
    default:
      return (
        <>
          {entry.origin.actor ? (
            <p className="mt-1 text-sm text-ink-muted">To <EntryLink id={entry.related_actor_id ?? entry.related_entry_id} nav={nav}>{entry.origin.actor}</EntryLink></p>
          ) : null}
          <Quote text={entry.text} nav={nav} />
          {(entry.related_entry_ids ?? []).filter((id) => nav.index.byId.has(id)).length ? (
            <p className="mt-2 text-xs text-ink-subtle">
              Connected to{" "}
              {(entry.related_entry_ids ?? []).filter((id) => nav.index.byId.has(id)).map((id, position) => (
                <Fragment key={id}>{position ? ", " : null}<EntryLink id={id} nav={nav}>{nav.index.byId.get(id)!.title}</EntryLink></Fragment>
              ))}
            </p>
          ) : null}
        </>
      );
  }
}

function EntryArticle({
  entry,
  nav,
  selected,
  showKind = false,
  embeddedNotes = true,
}: {
  entry: JournalEntry;
  nav: Navigation;
  selected: boolean;
  showKind?: boolean;
  embeddedNotes?: boolean;
}) {
  const headingId = `${entryDomId(entry.id)}-title`;
  const here = entry.category === "place" && entry.id === nav.currentPlaceId;
  const isNote = entry.category === "note";
  return (
    <article
      id={entryDomId(entry.id)}
      aria-labelledby={headingId}
      aria-current={selected ? "true" : undefined}
      tabIndex={-1}
      className={`scroll-mt-4 border-l-2 py-3 pl-3 outline-none focus-visible:ring-2 focus-visible:ring-ember-400 ${selected ? "border-ember-500 bg-ember-950/20" : "border-transparent"}`}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h3 id={headingId} className={`min-w-0 break-words text-ink ${isNote ? "font-sans text-sm font-medium leading-6" : "font-serif text-lg font-semibold leading-snug"}`}>
          {isNote ? entry.text : entry.title}
        </h3>
        <div className="flex shrink-0 items-center gap-1.5">
          {showKind ? <span className="text-xs text-ink-subtle">{CATEGORY_LABELS[entry.category]}</span> : null}
          {here ? <Pill tone="ember">You are here</Pill> : null}
          {!isNote && entry.status !== "discovered" && entry.status !== "encountered" ? (
            <Pill tone={statusTone(entry.status)}>{statusLabel(entry.status)}</Pill>
          ) : null}
        </div>
      </div>
      <Provenance entry={entry} nav={nav} verb={verbFor(entry)} />
      <EntryBody entry={entry} nav={nav} embeddedNotes={embeddedNotes} />
      <History entry={entry} nav={nav} />
    </article>
  );
}

function Paged({
  entries,
  pageKey,
  selectedEntryId,
  render,
}: {
  entries: JournalEntry[];
  pageKey: string;
  selectedEntryId: string | null;
  render: (entry: JournalEntry) => ReactNode;
}) {
  const [page, setPage] = useState<{ key: string; count: number }>({ key: "", count: PAGE_SIZE });
  // A new view or search starts at the beginning; a selected later entry is always revealed.
  const base = page.key === pageKey ? page.count : PAGE_SIZE;
  const selectedIndex = selectedEntryId ? entries.findIndex((entry) => entry.id === selectedEntryId) : -1;
  const visible = Math.max(base, selectedIndex + 1);
  return (
    <>
      <div className="divide-y divide-line">{entries.slice(0, visible).map(render)}</div>
      {entries.length > visible ? (
        <Button
          className="mt-3 w-full"
          variant="secondary"
          onClick={() => {
            setPage({ key: pageKey, count: Math.min(visible + PAGE_SIZE, entries.length) });
            focusEntry(entries[visible].id);
          }}
        >
          Show {Math.min(PAGE_SIZE, entries.length - visible)} more
        </Button>
      ) : null}
    </>
  );
}

/** The opening line of the log: where you stand and what you are following. */
function NowLine({ data, index, nav }: { data: JournalRead; index: JournalIndex; nav: Navigation }) {
  const place = data.current_place;
  const goal = data.active_goal;
  const lead = goal ? null : index.lead;
  return (
    <section aria-label="Where you stand" className="border-b border-line pb-4">
      <p className={`font-serif text-ink ${nav.largeText ? "text-xl leading-9" : "text-lg leading-8"}`}>
        {place ? <>You are at <EntryLink id={place.id} nav={nav}>{place.title}</EntryLink></> : <>Your whereabouts are not recorded yet.</>}
      </p>
      {goal || lead ? (
        <div className="mt-3">
          <p className="text-xs font-medium text-ink-subtle">{goal ? "Current goal" : "Latest lead"}</p>
          <p className={`mt-1 font-serif text-ink ${nav.largeText ? "text-lg leading-8" : "text-base leading-7"}`}>
            <EntryLink id={(goal ?? lead)!.id} nav={nav}>
              {goal?.title ?? (lead?.category === "note" ? lead.text ?? lead.title : lead?.title)}
            </EntryLink>
          </p>
        </div>
      ) : <p className="mt-2 text-sm text-ink-muted">No goal or lead has been recorded yet.</p>}
    </section>
  );
}

function Overview({ data, index, nav, onView }: { data: JournalRead; index: JournalIndex; nav: Navigation; onView: (view: JournalView) => void }) {
  const recent = index.chronological.filter((entry) => entry.category !== "place" || entry.status !== "visited").slice(0, 5);
  const counts: [JournalView, number, string][] = [
    ["people", index.people.length, index.people.length === 1 ? "person met" : "people met"],
    ["places", index.places.length, index.places.length === 1 ? "place" : "places"],
    ["clues", index.clues.length + index.looseNotes.length, index.clues.length + index.looseNotes.length === 1 ? "clue or note" : "clues and notes"],
    ["goals", index.goals.length, index.goals.length === 1 ? "goal or promise" : "goals and promises"],
  ];
  return (
    <div className="space-y-5">
      <NowLine data={data} index={index} nav={nav} />
      {index.threads.length ? (
        <section aria-labelledby="journal-threads">
          <h3 id="journal-threads" className="font-serif text-base font-semibold text-ink">Still open</h3>
          <ul className="mt-2 space-y-1.5 text-sm">
            {index.threads.map((entry) => (
              <li key={entry.id} className="flex flex-wrap items-baseline gap-x-2">
                <EntryLink id={entry.id} nav={nav}>{entry.title}</EntryLink>
                <span className="text-xs text-ink-subtle">{entry.category === "place" ? "a place you have heard of" : statusLabel(entry.status)}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {recent.length ? (
        <section aria-labelledby="journal-recent">
          <h3 id="journal-recent" className="font-serif text-base font-semibold text-ink">Most recently</h3>
          <ul className="mt-2 space-y-2 text-sm">
            {recent.map((entry) => (
              <li key={entry.id} className="grid grid-cols-[3.5rem_1fr] gap-2">
                <span className="text-xs leading-6 text-ink-subtle">{lastRecordedTurn(entry) >= 0 ? `Turn ${lastRecordedTurn(entry)}` : ""}</span>
                <span className="min-w-0 break-words leading-6">
                  <EntryLink id={entry.id} nav={nav}>{entry.title}</EntryLink>
                  <span className="text-xs text-ink-subtle"> ({CATEGORY_LABELS[entry.category].toLowerCase()})</span>
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      <nav aria-label="Journal sections" className="flex flex-wrap gap-2">
        {counts.map(([view, count, label]) => (
          <Button key={view} variant="secondary" size="sm" className="min-h-11" onClick={() => onView(view)}>
            {count} {label}
          </Button>
        ))}
      </nav>
    </div>
  );
}

/** Log: the adventure as a thread of turns, newest first. Turn numbers are a real sequence here. */
function Log({ index, nav, selectedEntryId, pageKey }: { index: JournalIndex; nav: Navigation; selectedEntryId: string | null; pageKey: string }) {
  const entries = index.chronological;
  return (
    <Paged
      entries={entries}
      pageKey={pageKey}
      selectedEntryId={selectedEntryId}
      render={(entry) => {
        const position = entries.indexOf(entry);
        const turn = lastRecordedTurn(entry);
        const newTurn = position === 0 || lastRecordedTurn(entries[position - 1]) !== turn;
        return (
          <div key={entry.id} className={newTurn ? "pt-2" : "border-t-0"}>
            {newTurn ? (
              <h3 className="pt-2 text-xs font-medium text-ink-subtle">
                {turn >= 0 ? `Turn ${turn}` : "Earlier"}
              </h3>
            ) : null}
            <EntryArticle entry={entry} nav={nav} selected={selectedEntryId === entry.id} showKind embeddedNotes={false} />
          </div>
        );
      }}
    />
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
  view,
  onViewChange,
  query,
  onQueryChange,
  onOpenHistory,
  largeText = false,
}: JournalPanelProps) {
  const index = useMemo(() => indexJournal(data), [data]);
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const results = useMemo(
    () => (normalizedQuery ? index.chronological.filter((entry) => matchesQuery(entry, normalizedQuery)) : []),
    [index, normalizedQuery],
  );
  const searchRef = useRef<HTMLInputElement | null>(null);
  const params = new URLSearchParams();
  if (query.trim()) params.set("q", query.trim());
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const exportUrl = data ? `${apiBase}/api/sessions/${data.session_id}/journal/export${suffix}` : "";

  const openEntry = (id: string) => {
    const entry = index.byId.get(id);
    if (!entry) return;
    onQueryChange("");
    onViewChange(viewFor(entry.category));
    onSelectedEntryChange(id);
    focusEntry(id);
  };
  const nav: Navigation = { index, currentPlaceId: data?.current_place?.id ?? null, openEntry, onOpenHistory, largeText, selectedEntryId };

  // An entry opened from elsewhere (header, narration, map) is shown in its own section.
  const selected = selectedEntryId ? index.byId.get(selectedEntryId) : undefined;
  const selectedView = selected ? viewFor(selected.category) : null;
  const selectedAvailable = Boolean(selected);
  const revealedFor = useRef<string | null>(null);
  useEffect(() => {
    if (!selected || !selectedView || revealedFor.current === selected.id) return;
    revealedFor.current = selected.id;
    if (normalizedQuery) return;
    if (view !== selectedView && view !== "log") onViewChange(selectedView);
  }, [selected, selectedView, view, normalizedQuery, onViewChange]);
  useEffect(() => {
    if (!selectedEntryId || !selectedAvailable) return;
    const frame = requestAnimationFrame(() => {
      document.getElementById(entryDomId(selectedEntryId))?.scrollIntoView?.({ block: "nearest" });
    });
    return () => cancelAnimationFrame(frame);
  }, [selectedEntryId, selectedAvailable, view]);

  const onSearchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (event.key === "Escape" && query) {
      // Clear the search first; a second Escape closes the journal.
      event.preventDefault();
      event.stopPropagation();
      onQueryChange("");
    }
  };

  const counts: Partial<Record<JournalView, number>> = {
    people: index.people.length,
    places: index.places.length,
    clues: index.clues.length + index.looseNotes.length,
    goals: index.goals.length,
  };
  const tabs = JOURNAL_VIEWS.map((id) => ({
    id,
    label: counts[id] ? <>{VIEW_LABELS[id]} <span className="text-ink-subtle">{counts[id]}</span></> : VIEW_LABELS[id],
  }));
  const pageKey = `${data?.session_id ?? "none"}:${view}`;
  const revealEntryId = !normalizedQuery && view === "clues" && selected?.category === "note"
    ? index.clueForNote(selected.id)?.id ?? selectedEntryId : selectedEntryId;
  const article = (entry: JournalEntry, showKind = false) => (
    <EntryArticle key={entry.id} entry={entry} nav={nav} selected={selectedEntryId === entry.id} showKind={showKind} embeddedNotes={!normalizedQuery} />
  );
  const empty = data !== null && data.entries.length === 0;

  return (
    <section aria-labelledby="journal-heading" className="space-y-4 [overflow-wrap:anywhere]">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="journal-heading" className="sr-only">Journal</h2>
        <form role="search" className="min-w-0 flex-1" onSubmit={(event) => event.preventDefault()}>
          <label htmlFor="journal-search" className="sr-only">Search the journal</label>
          <Input
            id="journal-search"
            ref={searchRef}
            className="min-h-11"
            type="search"
            placeholder="Search names, places, clues"
            value={query}
            onChange={(event) => onQueryChange(event.target.value)}
            onKeyDown={onSearchKeyDown}
          />
        </form>
        <Button variant="secondary" size="sm" className="min-h-11" loading={loading} onClick={onRefresh}>Refresh</Button>
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
          This session predates journal tracking. New discoveries will appear here as you play.
        </p>
      ) : null}

      <p role="status" aria-live="polite" className="sr-only">
        {normalizedQuery ? `${results.length} ${results.length === 1 ? "entry matches" : "entries match"} “${query.trim()}”.` : ""}
      </p>

      {data && normalizedQuery ? (
        <section aria-labelledby="journal-results">
          <h3 id="journal-results" className="text-sm text-ink-muted">
            {results.length ? `${results.length} ${results.length === 1 ? "entry matches" : "entries match"} “${query.trim()}”` : `Nothing in your journal matches “${query.trim()}”.`}
          </h3>
          {results.length ? (
            <Paged entries={results} pageKey={`${pageKey}:q:${normalizedQuery}`} selectedEntryId={selectedEntryId} render={(entry) => article(entry, true)} />
          ) : (
            <Button className="mt-2 min-h-11" variant="secondary" size="sm" onClick={() => { onQueryChange(""); searchRef.current?.focus(); }}>Clear search</Button>
          )}
        </section>
      ) : null}

      {data && !normalizedQuery ? (
        empty ? (
          <EmptyState title="Nothing recorded yet">
            People you meet, places you reach, clues you find and what you learn will be written here as you play.
          </EmptyState>
        ) : (
          <div>
            <Tabs items={tabs} value={view} onChange={(next) => { onSelectedEntryChange(null); onViewChange(next); }} idBase={ID_BASE} label="Journal sections" className="-mx-1" />
            <TabPanel idBase={ID_BASE} id={view} className="pt-4">
              {view === "overview" ? <Overview data={data} index={index} nav={nav} onView={onViewChange} /> : null}
              {view === "log" ? <Log index={index} nav={nav} selectedEntryId={selectedEntryId} pageKey={pageKey} /> : null}
              {view !== "overview" && view !== "log" ? (
                entriesForView(index, view).length ? (
                  <Paged entries={entriesForView(index, view)} pageKey={pageKey} selectedEntryId={revealEntryId} render={(entry) => article(entry)} />
                ) : (
                  <p className="text-sm text-ink-muted">
                    {view === "people" ? "No one has been met yet." : view === "places" ? "No places have been recorded yet." : view === "clues" ? "No clues have been found yet." : "No goals or promises have been recorded yet."}
                  </p>
                )
              ) : null}
            </TabPanel>
          </div>
        )
      ) : null}

      {data ? (
        <p className="border-t border-line pt-3 text-xs text-ink-subtle">
          Session {data.session_id}.{" "}
          <ApiDownloadLink
            url={exportUrl}
            filename={`journal-session-${data.session_id}.json`}
            plainDownload
            className="font-medium text-ember-300 underline decoration-ember-400/60 underline-offset-2 hover:text-ember-200"
          >
            {query.trim() ? "Download matching entries" : "Download journal"}
          </ApiDownloadLink>
        </p>
      ) : null}
    </section>
  );
}

export default JournalPanel;
