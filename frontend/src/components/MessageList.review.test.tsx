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

it("keeps the log silent while narration streams and shows a failed turn as a labelled group, not a second alert", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  const pending = { turnId: "t1", requestId: "r1", input: "look", stage: "narrating", text: "The door creaks", adjudication: null, debug: null };
  try {
    await act(() => root.render(
      <MessageList
        messages={[{ id: "h0", role: "dm", content: "Old saved narration." }]}
        pending={pending}
      />,
    ));
    const log = container.querySelector('[role="log"]')!;
    expect(log.getAttribute("aria-live")).toBe("off");
    expect(log.textContent).toContain("The door creaks");
    expect(container.querySelectorAll("[aria-live]:not([aria-live='off']), [role='status'], [role='alert']")).toHaveLength(0);

    await act(() => root.render(
      <MessageList
        messages={[
          { id: "h0", role: "dm", content: "Old saved narration." },
          { id: "t1:error", role: "error", content: "Connection lost" },
        ]}
        pending={null}
      />,
    ));
    // The Play status region announces the failure once; the row itself must not announce it again.
    expect(container.querySelectorAll('[role="alert"]')).toHaveLength(0);
    expect(container.querySelector('[role="group"][aria-label="Action failed"]')?.textContent).toContain("Connection lost");
  } finally {
    await act(() => root.unmount()); container.remove(); vi.unstubAllGlobals();
  }
});

it("offers paid retry only after a definitive retryable failure", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  try {
    await act(() => root.render(<MessageList messages={[
      {id:"internal",turnId:"internal",role:"error",content:"Internal failure",code:"turn_failed",retryInput:"Look",retryRequestId:"r1",retryable:false},
      {id:"provider",turnId:"provider",role:"error",content:"Provider failure",code:"provider_unavailable",retryInput:"Wait",retryRequestId:"r2",retryable:true},
      {id:"unknown",turnId:"unknown",role:"error",content:"Save uncertain",code:"turn_save_failed",retryInput:"Go",retryRequestId:"r3",uncertain:true},
    ]} pending={null} onRetry={vi.fn()} onCheckReceipt={vi.fn()} />));
    expect([...container.querySelectorAll('button')].filter(b=>b.textContent==='Retry')).toHaveLength(1);
    expect([...container.querySelectorAll('button')].filter(b=>b.textContent==='Check save status')).toHaveLength(1);
  } finally {
    await act(()=>root.unmount()); container.remove(); vi.unstubAllGlobals();
  }
});
