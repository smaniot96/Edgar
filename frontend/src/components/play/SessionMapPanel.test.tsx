// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

import type { SessionMapRead } from "../../api/types";
import type { JournalEntry } from "../journal/types";
import { SessionMapPanel } from "./SessionMapPanel";

const map: SessionMapRead = {
  session_id: 12,
  source_version: "a".repeat(64),
  topology: "prepared",
  schematic: true,
  scale: null,
  current_scene_id: "harbor",
  places: [
    { scene_id: "harbor", title: "The harbor", status: "current", detail: null },
    { scene_id: "watchtower", title: "The watchtower", status: "known", detail: "A narrow stair climbs above the pier." },
  ],
  routes: [{ from_scene_id: "harbor", to_scene_id: "watchtower" }],
  note: "Schematic only.",
  journal_entry_ids: { harbor: "entry-harbor", watchtower: "entry-watchtower" },
  known_place_entries: [],
  artwork: [],
  tracking: "available",
};

it("fits and returns to the current place, and travel only calls the draft callback", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const onPrepareTravel = vi.fn();
  const onOpenJournal = vi.fn();
  const onSubmit = vi.fn();
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(() => root.render(
      <form onSubmit={onSubmit}>
        <SessionMapPanel
          sessionId={12}
          map={map}
          loading={false}
          error={null}
          onRefresh={vi.fn()}
          onOpenJournal={onOpenJournal}
          onPrepareTravel={onPrepareTravel}
        />
      </form>,
    ));

    for (const label of ["Fit map", "Current place", "Refresh map"]) {
      const button = [...container.querySelectorAll("button")].find((item) => item.textContent === label);
      expect(button?.className).toContain("min-h-11");
    }
    expect(container.querySelector<HTMLButtonElement>('[aria-label="Zoom in"]')?.className).toContain("min-w-11");

    await act(() => container.querySelector<HTMLButtonElement>('button[aria-label="Zoom in"]')!.click());
    expect(container.querySelector("figure > div")?.getAttribute("style")).toContain("125%");
    await act(() => [...container.querySelectorAll("button")].find((button) => button.textContent === "Fit map")!.click());
    expect(container.querySelector("figure > div")?.getAttribute("style")).toContain("100%");

    await act(() => [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("The watchtower"))!.click());
    expect(container.textContent).toContain("A narrow stair climbs above the pier.");
    await act(() => [...container.querySelectorAll("button")].find((button) => button.textContent === "Add travel idea to draft")!.click());
    expect([...container.querySelectorAll("button")].find((button) => button.textContent === "Add travel idea to draft")?.className).toContain("min-h-11");
    expect(onPrepareTravel).toHaveBeenCalledExactlyOnceWith("The watchtower");
    expect(onSubmit).not.toHaveBeenCalled();
    expect(container.querySelector('[aria-labelledby="known-routes-heading"]')?.textContent).toContain("The harbor to The watchtower");

    await act(() => [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Current place"))!.click());
    expect(container.querySelector('button[aria-pressed="true"]')?.textContent).toContain("The harbor");
    expect(localStorage.getItem(`edgar_play_map:v1:12:${map.source_version}`)).toContain('"sceneId":"harbor"');
  } finally {
    await act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    localStorage.clear();
  }
});

it("restores pan on reopen and inspecting a visual pin never prepares or submits travel", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const onPrepareTravel = vi.fn();
  const panel = <SessionMapPanel sessionId={12} map={map} loading={false} error={null}
    onRefresh={vi.fn()} onOpenJournal={vi.fn()} onPrepareTravel={onPrepareTravel} />;
  try {
    await act(() => root.render(panel));
    await act(() => container.querySelector<HTMLButtonElement>('[aria-label="Inspect The watchtower"]')!.click());
    expect(container.textContent).toContain("A narrow stair climbs above the pier.");
    expect(onPrepareTravel).not.toHaveBeenCalled();
    const figure = container.querySelector("figure")!;
    await act(() => {
      figure.scrollLeft = 72;
      figure.scrollTop = 24;
      figure.dispatchEvent(new Event("scroll"));
    });
    await act(() => root.render(null));
    await act(() => root.render(panel));
    await act(() => container.querySelector("img")!.dispatchEvent(new Event("load")));
    expect(container.querySelector("figure")?.scrollLeft).toBe(72);
    expect(container.querySelector("figure")?.scrollTop).toBe(24);
    expect(container.querySelector('[aria-label="Inspect The watchtower"]')?.getAttribute("aria-pressed")).toBe("true");
    await act(() => container.querySelector("img")!.dispatchEvent(new Event("error")));
    expect(container.textContent).toContain("The map image is unavailable or changed.");
    expect(container.querySelector("figure button")).toBeNull();
    expect(container.querySelector('[aria-labelledby="known-routes-heading"]')?.textContent).toContain("The harbor to The watchtower");
  } finally {
    await act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    localStorage.clear();
  }
});

it("loads the saved view for a changed map version instead of carrying the previous view", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const nextVersion = "b".repeat(64);
  localStorage.setItem(
    `edgar_play_map:v1:12:${nextVersion}`,
    JSON.stringify({ sceneId: "watchtower", zoom: 1.5 }),
  );
  const renderPanel = (version: string) => (
    <SessionMapPanel
      sessionId={12}
      map={{ ...map, source_version: version }}
      loading={false}
      error={null}
      onRefresh={vi.fn()}
      onOpenJournal={vi.fn()}
      onPrepareTravel={vi.fn()}
    />
  );
  try {
    await act(() => root.render(renderPanel(map.source_version)));
    await act(() => root.render(renderPanel(nextVersion)));
    expect(container.textContent).toContain("150%");
    expect(container.querySelector('button[aria-pressed="true"]')?.textContent).toContain("The watchtower");
  } finally {
    await act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    localStorage.clear();
  }
});

it("opens only the supplied player-safe people from the selected session", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const onOpenJournal = vi.fn();
  const actor: JournalEntry = {
    id: "actor_session_12", actor_id: "actor_session_12", actor_aliases: ["Mara"],
    actor_reference_event_ids: [31], category: "person", title: "Mara · person encountered on turn 1",
    text: "A guard named Mara asks you to wait.", status: "encountered", quantity: null,
    origin: { turn_number: 1, history_event_id: 31, place: null, actor: null, citation: null },
    history: [], related_entry_id: null,
  };
  try {
    await act(() => root.render(
      <SessionMapPanel
        sessionId={12} map={map} loading={false} error={null}
        onRefresh={vi.fn()} onOpenJournal={onOpenJournal} onPrepareTravel={vi.fn()}
        actors={[actor]}
      />,
    ));
    expect(container.textContent).toContain("Known people");
    const link = [...container.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent?.includes(actor.title));
    expect(link?.textContent).toContain("NPC ·");
    expect(link?.textContent).toContain("Also known as Mara");
    await act(() => link!.click());
    expect(onOpenJournal).toHaveBeenCalledExactlyOnceWith(actor.id);
  } finally {
    await act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    localStorage.clear();
  }
});
