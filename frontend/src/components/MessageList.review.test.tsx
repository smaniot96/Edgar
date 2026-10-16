// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { MessageList } from "./MessageList";
import type { JournalEntry } from "./journal/types";
vi.mock("../dev/ModeContext", () => ({ useMode: () => ({ isDev: false }) }));

it("adds accessible links beside saved narration without changing its text", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  const onOpenActorEntry = vi.fn();
  const actor: JournalEntry = {
    id: "entry_person_1", actor_id: "entry_person_1", actor_aliases: ["Mara"],
    actor_reference_event_ids: [8], category: "person", title: "Mara · person encountered on turn 1",
    text: "A guard named Mara asks you to wait.", status: "encountered", quantity: null,
    origin: { turn_number: 1, history_event_id: 8, place: null, actor: null, citation: null },
    history: [], related_entry_id: null,
  };
  try {
    await act(() => root.render(
      <MessageList
        messages={[
          { id: "turn-8", role: "dm", eventId: 8, content: "Mara opens the gate." },
          { id: "turn-9", role: "dm", eventId: 9, content: "The road bends north." },
        ]}
        pending={null}
        actorEntries={[actor]}
        onOpenActorEntry={onOpenActorEntry}
      />,
    ));

    const links = [...container.querySelectorAll<HTMLButtonElement>('button[aria-label^="Open journal entry for NPC"]')];
    expect(links).toHaveLength(1);
    expect(links[0].textContent).toBe(`NPC · ${actor.title}`);
    expect(container.textContent).toContain("Mara opens the gate.");
    expect(container.textContent).toContain("The road bends north.");
    await act(() => links[0].click());
    expect(onOpenActorEntry).toHaveBeenCalledExactlyOnceWith(actor.id);
  } finally {
    await act(() => root.unmount()); container.remove(); vi.unstubAllGlobals();
  }
});

it("leaves the persisted reading anchor untouched until boot history is seeded", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  const key = "review-anchor";
  const saved = JSON.stringify({ messageId: "old", eventId: 3, offset: -20, scrollTop: 513.5 });
  sessionStorage.setItem(key, saved);
  try {
    await act(() => root.render(<MessageList messages={[]} pending={null} storageKey={key} restoreReady={false} />));
    expect(sessionStorage.getItem(key)).toBe(saved);
    await act(() => container.querySelector('[role="log"]')!.dispatchEvent(new Event("scroll", { bubbles: true })));
    expect(sessionStorage.getItem(key)).toBe(saved);
  } finally {
    await act(() => root.unmount()); container.remove(); sessionStorage.removeItem(key); vi.unstubAllGlobals();
  }
});
