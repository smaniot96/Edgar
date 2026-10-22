import { useEffect, useMemo, useRef, useState } from "react";

import { apiBase, errorMessage } from "../../api/client";
import { useAuthorizedObjectUrl } from "../../api/resources";
import type { SessionMapRead, SessionWorldMap } from "../../api/types";
import type { JournalEntry } from "../journal/types";
import { ApiDownloadLink } from "../ui/ApiDownloadLink";
import { AuthorizedImage } from "../ui/AuthorizedImage";
import { Button } from "../ui/Button";

type SavedView = { sceneId: string | null; zoom: number; panLeft: number; panTop: number; scrollTop: number };

const emptyView: SavedView = { sceneId: null, zoom: 1, panLeft: 0, panTop: 0, scrollTop: 0 };

/** A short, safe file extension from a media type ("application/pdf" -> "pdf"). */
function handoutExtension(mediaType: string): string {
  const subtype = mediaType.split("/")[1]?.split("+")[0]?.replace(/[^a-z0-9]/gi, "").toLowerCase() ?? "";
  return subtype.length > 0 && subtype.length <= 8 ? subtype : "bin";
}

function savedOffset(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.min(value, 1_000_000)) : 0;
}

function viewKey(sessionId: number, sourceVersion: string) {
  return `edgar_play_map:v1:${sessionId}:${sourceVersion}`;
}

function readView(sessionId: number, sourceVersion: string): SavedView {
  try {
    const raw = JSON.parse(localStorage.getItem(viewKey(sessionId, sourceVersion)) ?? "null") as Partial<SavedView> | null;
    return {
      sceneId: typeof raw?.sceneId === "string" ? raw.sceneId : null,
      zoom: raw?.zoom === 1.25 || raw?.zoom === 1.5 ? raw.zoom : 1,
      panLeft: savedOffset(raw?.panLeft),
      panTop: savedOffset(raw?.panTop),
      scrollTop: savedOffset(raw?.scrollTop),
    };
  } catch {
    return emptyView;
  }
}

function placeStatus(status: string) {
  return status === "current" ? "Current place" : status === "visited" ? "Visited" : "Known place";
}

function artworkLabel(kind: string) {
  return kind === "original_creature_art" ? "Original source artwork"
    : kind === "source_map" ? "Retained source map"
    : kind === "generated_illustration" ? "Generated illustration · decorative"
    : "Player handout";
}

type SessionMapPanelProps = {
  sessionId: number;
  map: SessionMapRead | null;
  loading: boolean;
  error: unknown;
  onRefresh: () => void;
  onOpenJournal: (entryId: string) => void;
  onPrepareTravel: (place: string) => void;
  actors?: JournalEntry[];
};

const WORLD_STATUS: Record<SessionWorldMap["places"][number]["status"], string> = {
  current: "You are here",
  visited: "Visited",
  known: "Known",
  rumoured: "Known by reputation",
};

function WorldMapSection({ sessionId, sourceVersion, world }: { sessionId: number; sourceVersion: string; world: SessionWorldMap }) {
  const url = useMemo(
    () => `${apiBase}/api/sessions/${sessionId}/world-map.svg?v=${encodeURIComponent(sourceVersion)}`,
    [sessionId, sourceVersion],
  );
  const image = useAuthorizedObjectUrl(url);
  const [failed, setFailed] = useState(false);
  const labels = new Map(world.places.map((place) => [place.place_id, place.label]));
  const here = world.current_place_id ? labels.get(world.current_place_id) : null;
  return (
    <section aria-labelledby="world-map-heading">
      <h3 id="world-map-heading" className="font-serif text-lg font-semibold text-ink">{world.title}</h3>
      {here ? <p className="mt-1 text-sm text-ink-muted">You are at {here}.</p> : null}
      {!failed && !image.error ? (
        <figure className="mt-2 overflow-auto rounded-control border border-line bg-[#efe2c4]">
          <img
            src={image.objectUrl ?? undefined}
            onError={() => setFailed(true)}
            alt={`Sketch map of ${world.title}: ${world.places.map((place) => `${place.label} (${WORLD_STATUS[place.status].toLowerCase()})`).join(", ")}.`}
            className="h-auto w-full"
          />
        </figure>
      ) : (
        <p className="mt-2 text-sm text-ink-muted" role="status">The region map image is unavailable. Refresh the map, or use the list below.</p>
      )}
      <ul className="mt-3 grid gap-1 text-sm sm:grid-cols-2" aria-label="Places on the region map">
        {world.places.map((place) => (
          <li key={place.place_id} className="flex justify-between gap-2 rounded-control bg-surface px-2 py-1">
            <span className="text-ink">{place.label}</span>
            <span className="text-ink-muted">{WORLD_STATUS[place.status]}</span>
          </li>
        ))}
      </ul>
      {world.routes.length ? (
        <p className="mt-2 text-xs text-ink-muted">
          Ways known: {world.routes.map((route) => `${labels.get(route.from_place_id)} – ${labels.get(route.to_place_id)} (${route.label || route.kind})`).join("; ")}
        </p>
      ) : null}
    </section>
  );
}

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
  actors = [],
}: SessionMapPanelProps) {
  const mapPaneRef = useRef<HTMLDivElement | null>(null);
  const mapFigureRef = useRef<HTMLElement | null>(null);
  const selectedRef = useRef<HTMLLIElement | null>(null);
  const [view, setView] = useState<SavedView>(() => map ? readView(sessionId, map.source_version) : emptyView);
  const [imageFailed, setImageFailed] = useState(false);
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
  const columns = Math.max(1, Math.ceil(Math.sqrt(places.length)));
  const diagramWidth = Math.max(520, columns * 260 + 100);
  const diagramHeight = Math.max(220, Math.ceil(places.length / columns) * 150 + 100);
  const placeById = new Map(places.map((place) => [place.scene_id, place]));
  const mapUrl = useMemo(
    () => `${apiBase}/api/sessions/${sessionId}/map.svg?v=${encodeURIComponent(sourceVersion)}`,
    [sessionId, sourceVersion],
  );
  // The map is fetched with the bearer token and shown from an object URL (EDG-160).
  const mapImage = useAuthorizedObjectUrl(mapUrl);
  const mapImageFailed = imageFailed || Boolean(mapImage.error);

  function updateView(next: Partial<SavedView>) {
    setView((currentView) => ({ ...currentView, ...next }));
  }

  function fitMap() {
    updateView({ zoom: 1, panLeft: 0, panTop: 0, scrollTop: 0 });
    if (mapFigureRef.current) {
      mapFigureRef.current.scrollLeft = 0;
      mapFigureRef.current.scrollTop = 0;
    }
    if (mapPaneRef.current) mapPaneRef.current.scrollTop = 0;
  }

  function restoreScroll() {
    if (mapFigureRef.current) {
      mapFigureRef.current.scrollLeft = view.panLeft;
      mapFigureRef.current.scrollTop = view.panTop;
    }
    if (mapPaneRef.current) mapPaneRef.current.scrollTop = view.scrollTop;
  }

  function showCurrent() {
    const place = current ?? listPlaces[0];
    if (!place) return;
    updateView({ sceneId: place.scene_id });
    requestAnimationFrame(() => {
      if (selectedRef.current && "scrollIntoView" in selectedRef.current) {
        selectedRef.current.scrollIntoView({ block: "nearest" });
      }
    });
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div role="group" className="sticky top-0 z-10 flex shrink-0 flex-wrap items-center gap-2 border-b border-line bg-canvas px-3 py-2" aria-label="Map controls">
        <Button className="min-h-11" variant="secondary" size="sm" onClick={fitMap}>Fit map</Button>
        <Button className="min-h-11" variant="secondary" size="sm" onClick={showCurrent} disabled={!current && listPlaces.length === 0}>Current place</Button>
        <span className="ml-auto text-xs text-ink-muted" aria-live="polite">Zoom {Math.round(view.zoom * 100)}%</span>
        <Button className="min-h-11 min-w-11" variant="secondary" size="sm" aria-label="Zoom out" disabled={view.zoom <= 1} onClick={() => updateView({ zoom: Math.max(1, view.zoom - 0.25) })}>−</Button>
        <Button className="min-h-11 min-w-11" variant="secondary" size="sm" aria-label="Zoom in" disabled={view.zoom >= 1.5} onClick={() => updateView({ zoom: Math.min(1.5, view.zoom + 0.25) })}>+</Button>
        <Button className="min-h-11" variant="secondary" size="sm" onClick={onRefresh}>Refresh map</Button>
      </div>
      <div ref={mapPaneRef} onScroll={(event) => updateView({ scrollTop: event.currentTarget.scrollTop })} className="min-h-0 flex-1 overflow-y-auto px-4 py-4 pb-[max(1rem,env(safe-area-inset-bottom))] [overflow-wrap:anywhere]">
        {loading && !map ? <p className="text-sm text-ink-muted" role="status">Loading this session’s map…</p> : null}
        {error && !map ? (
          <section className="rounded-control border border-line bg-surface p-4" role="alert">
            <h3 className="font-serif text-lg font-semibold text-ink">Map unavailable</h3>
            <p className="mt-2 text-sm text-ink-muted">{errorMessage(error)}</p>
            <Button className="mt-3 min-h-11" variant="secondary" size="sm" onClick={onRefresh}>Try again</Button>
          </section>
        ) : null}
        {map?.world_map ? (
          <WorldMapSection sessionId={sessionId} sourceVersion={sourceVersion} world={map.world_map} />
        ) : null}
        {map?.world_map && places.length > 0 ? (
          <h3 className="mt-5 font-serif text-lg font-semibold text-ink">Scenes reached</h3>
        ) : null}
        {map && places.length > 0 ? (
          <figure ref={mapFigureRef} tabIndex={0} onScroll={(event) => updateView({ panLeft: event.currentTarget.scrollLeft, panTop: event.currentTarget.scrollTop })} className="max-h-[50dvh] overflow-auto rounded-control border border-line bg-[#101827] p-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-ember-400" aria-label="Schematic map of known places">
            <div className="relative mx-auto transition-[width] duration-150 motion-reduce:transition-none" style={{ width: `${Math.round(view.zoom * 100)}%`, minWidth: `${Math.round(view.zoom * 100)}%` }}>
              <img
                src={mapImage.objectUrl ?? undefined}
                onLoad={() => { setImageFailed(false); restoreScroll(); }}
                onError={() => setImageFailed(true)}
                alt="Schematic map showing only places and connections known in this session. It does not show distance or scale."
                className="h-auto w-full max-w-none"
              />
              {!mapImageFailed && places.map((place, index) => (
                <button key={place.scene_id} type="button" aria-label={`Inspect ${place.title}`} aria-pressed={selected?.scene_id === place.scene_id}
                  onClick={() => updateView({ sceneId: place.scene_id })}
                  className="absolute min-h-11 min-w-11 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-transparent hover:border-ember-300 focus-visible:border-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-ember-400"
                  style={{ left: `${(120 + (index % columns) * 260) / diagramWidth * 100}%`, top: `${(100 + Math.floor(index / columns) * 150) / diagramHeight * 100}%` }}>
                  <span className="sr-only">{place.title}</span>
                </button>
              ))}
            </div>
            <figcaption className="px-2 pb-1 text-xs text-slate-300">Schematic connections only. The map does not show distance or scale.</figcaption>
          </figure>
        ) : null}
        {mapImageFailed ? <p className="mt-3 text-sm text-ink-muted" role="status">The map image is unavailable or changed. Refresh the map, or use the known places and routes below.</p> : null}
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

        <section className="mt-5" aria-labelledby="known-routes-heading">
          <h3 id="known-routes-heading" className="font-serif text-lg font-semibold text-ink">Known routes</h3>
          {map?.routes.length ? (
            <ul className="mt-2 space-y-2 text-sm text-ink-muted">
              {map.routes.map((route) => (
                <li key={`${route.from_scene_id}:${route.to_scene_id}`}>
                  {placeById.get(route.from_scene_id)?.title} to {placeById.get(route.to_scene_id)?.title}
                </li>
              ))}
            </ul>
          ) : <p className="mt-2 text-sm text-ink-muted">No routes have been recorded for this session yet.</p>}
        </section>

        {actors.length ? (
          <section className="mt-5" aria-labelledby="known-people-heading">
            <h3 id="known-people-heading" className="font-serif text-lg font-semibold text-ink">Known people</h3>
            <ul className="mt-2 space-y-2">
              {actors.map((actor) => (
                <li key={actor.actor_id ?? actor.id}>
                  <button
                    type="button"
                    className="min-h-11 w-full rounded-control border border-line bg-surface px-3 py-2 text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-ember-400"
                    onClick={() => onOpenJournal(actor.id)}
                  >
                    <span className="block break-words font-medium">NPC · {actor.title}</span>
                    {actor.actor_aliases?.length ? (
                      <span className="block text-xs text-ink-muted">Also known as {actor.actor_aliases.join(", ")}</span>
                    ) : null}
                  </button>
                </li>
              ))}
            </ul>
          </section>
        ) : null}

        {map?.artwork.length ? (
          <section className="mt-6" aria-labelledby="session-artwork-heading">
            <h3 id="session-artwork-heading" className="font-serif text-lg font-semibold text-ink">Session artwork</h3>
            <ul className="mt-2 grid grid-cols-1 gap-3 sm:grid-cols-2">
              {map.artwork.map((asset) => (
                <li key={asset.asset_id} className="overflow-hidden rounded-control border border-line bg-surface">
                  {asset.media_type.startsWith("image/") ? (
                    <AuthorizedImage url={`${apiBase}${asset.url}`} alt="Artwork connected to a known place or encountered creature" className="max-h-56 w-full object-contain" loading="lazy" />
                  ) : (
                    <ApiDownloadLink url={`${apiBase}${asset.url}`} filename={`player-handout.${handoutExtension(asset.media_type)}`} className="block w-full p-3 text-left text-sm text-ember-300 underline">
                      Open player handout
                    </ApiDownloadLink>
                  )}
                  <p className="px-3 pt-2 text-sm text-ink">{artworkLabel(asset.kind)}</p>
                  <p className="px-3 pb-3 text-xs text-ink-muted">Version {asset.sha256.slice(0, 12)}</p>
                </li>
              ))}
            </ul>
          </section>
        ) : null}
      </div>
    </div>
  );
}
