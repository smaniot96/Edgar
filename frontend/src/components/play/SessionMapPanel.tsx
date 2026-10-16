import { useEffect, useMemo, useRef, useState } from "react";

import { apiBase, errorMessage } from "../../api/client";
import type { SessionMapRead } from "../../api/types";
import { Button } from "../ui/Button";

type SavedView = { sceneId: string | null; zoom: number };

function viewKey(sessionId: number, sourceVersion: string) {
  return `edgar_play_map:v1:${sessionId}:${sourceVersion}`;
}

function readView(sessionId: number, sourceVersion: string): SavedView {
  try {
    const raw = JSON.parse(localStorage.getItem(viewKey(sessionId, sourceVersion)) ?? "null") as Partial<SavedView> | null;
    return {
      sceneId: typeof raw?.sceneId === "string" ? raw.sceneId : null,
      zoom: raw?.zoom === 1.25 || raw?.zoom === 1.5 ? raw.zoom : 1,
    };
  } catch {
    return { sceneId: null, zoom: 1 };
  }
}

function placeStatus(status: string) {
  return status === "current" ? "Current place" : status === "visited" ? "Visited" : "Known place";
}

type SessionMapPanelProps = {
  sessionId: number;
  map: SessionMapRead | null;
  loading: boolean;
  error: unknown;
  onRefresh: () => void;
  onOpenJournal: (entryId: string) => void;
  onPrepareTravel: (place: string) => void;
};

export function SessionMapPanel(props: SessionMapPanelProps) {
  const sourceVersion = props.map?.source_version ?? "loading";
  return <SessionMapPanelContent key={`${props.sessionId}:${sourceVersion}`} {...props} />;
}

function SessionMapPanelContent({
  sessionId,
  map,
  loading,
  error,
  onRefresh,
  onOpenJournal,
  onPrepareTravel,
}: SessionMapPanelProps) {
  const mapPaneRef = useRef<HTMLDivElement | null>(null);
  const selectedRef = useRef<HTMLLIElement | null>(null);
  const [view, setView] = useState<SavedView>(() => map ? readView(sessionId, map.source_version) : { sceneId: null, zoom: 1 });
  const sourceVersion = map?.source_version ?? "loading";
  const mapSourceVersion = map?.source_version;

  useEffect(() => {
    if (!mapSourceVersion) return;
    try { localStorage.setItem(viewKey(sessionId, mapSourceVersion), JSON.stringify(view)); } catch { /* best effort */ }
  }, [mapSourceVersion, sessionId, view]);

  const places = map?.places ?? [];
  const listPlaces = places.length
    ? places
    : (map?.known_place_entries ?? []).map((entry) => ({
      scene_id: entry.id,
      title: entry.title,
      status: "known" as const,
      detail: entry.text,
    }));
  const selected = listPlaces.find((place) => place.scene_id === view.sceneId) ?? null;
  const current = places.find((place) => place.scene_id === map?.current_scene_id) ?? null;
  const mapUrl = useMemo(
    () => `${apiBase}/api/sessions/${sessionId}/map.svg?v=${encodeURIComponent(sourceVersion)}`,
    [sessionId, sourceVersion],
  );

  function updateView(next: Partial<SavedView>) {
    setView((currentView) => ({ ...currentView, ...next }));
  }

  function fitMap() {
    updateView({ zoom: 1 });
    if (mapPaneRef.current) mapPaneRef.current.scrollTop = 0;
  }

  function showCurrent() {
    const place = current ?? listPlaces[0];
    if (!place) return;
    updateView({ sceneId: place.scene_id });
    requestAnimationFrame(() => {
      if (selectedRef.current && "scrollIntoView" in selectedRef.current) {
        selectedRef.current.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    });
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="sticky top-0 z-10 flex shrink-0 flex-wrap items-center gap-2 border-b border-line bg-canvas px-3 py-2" aria-label="Map controls">
        <Button className="min-h-11" variant="secondary" size="sm" onClick={fitMap}>Fit map</Button>
        <Button className="min-h-11" variant="secondary" size="sm" onClick={showCurrent} disabled={!current && listPlaces.length === 0}>Current place</Button>
        <span className="ml-auto text-xs text-ink-muted" aria-live="polite">{Math.round(view.zoom * 100)}%</span>
        <Button className="min-h-11 min-w-11" variant="secondary" size="sm" aria-label="Zoom out" disabled={view.zoom <= 1} onClick={() => updateView({ zoom: Math.max(1, view.zoom - 0.25) })}>−</Button>
        <Button className="min-h-11 min-w-11" variant="secondary" size="sm" aria-label="Zoom in" disabled={view.zoom >= 1.5} onClick={() => updateView({ zoom: Math.min(1.5, view.zoom + 0.25) })}>+</Button>
        <Button className="min-h-11" variant="secondary" size="sm" onClick={onRefresh}>Refresh map</Button>
      </div>
      <div ref={mapPaneRef} className="min-h-0 flex-1 overflow-y-auto px-4 py-4 pb-[max(1rem,env(safe-area-inset-bottom))]">
        {loading && !map ? <p className="text-sm text-ink-muted" role="status">Loading this session’s map…</p> : null}
        {error && !map ? (
          <section className="rounded-control border border-line bg-surface p-4" role="alert">
            <h3 className="font-serif text-lg font-semibold text-ink">Map unavailable</h3>
            <p className="mt-2 text-sm text-ink-muted">{errorMessage(error)}</p>
            <Button className="mt-3 min-h-11" variant="secondary" size="sm" onClick={onRefresh}>Try again</Button>
          </section>
        ) : null}
        {map && places.length > 0 ? (
          <figure className="overflow-auto rounded-control border border-line bg-[#101827] p-2" aria-label="Schematic map of known places">
            <img
              src={mapUrl}
              alt="Schematic map showing only places and connections known in this session. It does not show distance or scale."
              className="mx-auto h-auto max-w-none transition-[width] duration-150"
              style={{ width: `${Math.round(view.zoom * 100)}%`, minWidth: `${Math.round(view.zoom * 100)}%` }}
            />
            <figcaption className="px-2 pb-1 text-xs text-slate-300">Schematic connections only. The map does not show distance or scale.</figcaption>
          </figure>
        ) : null}
        {map?.note ? <p className="mt-3 text-sm text-ink-muted">{map.note}</p> : null}
        {map?.tracking === "legacy_untracked" ? <p className="mt-3 text-sm text-ink-muted">This older session has no recorded place knowledge yet.</p> : null}

        <section className="mt-5" aria-labelledby="known-places-heading">
          <h3 id="known-places-heading" className="font-serif text-lg font-semibold text-ink">Known places</h3>
          {listPlaces.length === 0 ? (
            <p className="mt-2 text-sm text-ink-muted">Places appear here after the story records them for this session.</p>
          ) : (
            <ul className="mt-2 space-y-2">
              {listPlaces.map((place) => {
                const journalId = map?.journal_entry_ids[place.scene_id];
                const active = selected?.scene_id === place.scene_id;
                return (
                  <li key={place.scene_id} ref={active ? selectedRef : null}>
                    <button
                      type="button"
                      aria-pressed={active}
                      onClick={() => updateView({ sceneId: place.scene_id })}
                      className={`w-full rounded-control border px-3 py-2 text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-ember-400 ${active ? "border-ember-500 bg-surface-raised text-ink" : "border-line bg-surface text-ink hover:bg-surface-raised"}`}
                    >
                      <span className="block font-medium">{place.title}</span>
                      <span className="block text-xs text-ink-muted">{placeStatus(place.status)}</span>
                    </button>
                    {active ? (
                      <div className="mt-2 flex flex-wrap gap-2 pl-2">
                        {place.detail ? <p className="w-full text-sm text-ink-muted">{place.detail}</p> : null}
                        {journalId ? <Button className="min-h-11" variant="secondary" size="sm" onClick={() => onOpenJournal(journalId)}>Open journal entry</Button> : null}
                        <Button className="min-h-11" variant="secondary" size="sm" onClick={() => onPrepareTravel(place.title)}>Add travel idea to draft</Button>
                      </div>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )}
        </section>

        {map?.artwork.length ? (
          <section className="mt-6" aria-labelledby="session-artwork-heading">
            <h3 id="session-artwork-heading" className="font-serif text-lg font-semibold text-ink">Session artwork</h3>
            <ul className="mt-2 grid grid-cols-1 gap-3 sm:grid-cols-2">
              {map.artwork.map((asset) => (
                <li key={asset.asset_id} className="overflow-hidden rounded-control border border-line bg-surface">
                  {asset.media_type.startsWith("image/") ? (
                    <img src={`${apiBase}${asset.url}`} alt="Artwork connected to a known place or encountered creature" className="max-h-56 w-full object-contain" loading="lazy" />
                  ) : (
                    <a className="block p-3 text-sm text-ember-700 underline" href={`${apiBase}${asset.url}`}>Open player handout</a>
                  )}
                </li>
              ))}
            </ul>
          </section>
        ) : null}
      </div>
    </div>
  );
}
