// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { CampaignRead } from "../api/types";
import type { JournalRead } from "../components/journal/types";
import Play from "./Play";
const harness = vi.hoisted(() => ({
  game: null as unknown,
  dispatch: vi.fn(),
  busy: false,
  historyReady: true,
  announcement: "",
  recover: vi.fn(),
  turnOptions: null as null | { onResult?: (result: unknown) => void; onAcknowledged?: (revision: number, requestId: string) => void },
  journal: { data: null as unknown, loading: false, error: null as unknown, stale: false, settledRevision: 0, refresh: vi.fn() },
}));
vi.mock("../dev/ModeContext", () => ({ useMode: () => ({ isDev: false }) }));
vi.mock("../components/play/usePlaySession", () => ({ usePlaySession: () => harness.game }));
vi.mock("../components/play/useTurn", () => ({ useTurn: (_sessionId: number, options: typeof harness.turnOptions) => { harness.turnOptions = options; return { dispatch: harness.dispatch, send: vi.fn(), stop: vi.fn(), retry: vi.fn(), recover: harness.recover, checkReceipt: vi.fn(), busy: harness.busy, state: { historyReady: harness.historyReady, lines: [{ id: "live", role: "dm", content: "A recently saved turn" }], pending: null, announcement: harness.announcement } }; } }));
vi.mock("../components/journal/api", () => ({ useJournal: () => harness.journal }));
vi.mock("../components/MessageList", () => ({ MessageList: (props: { openEventId?: number | null }) => <p data-open-event-id={props.openEventId ?? "none"}>A recently saved turn</p> }));
const campaign: CampaignRead = { id: 2, title: "Lost module", system: "5e", created_by: 1, created_at: "2026-10-14", adventure_collections: ["lost_module"], source_mode: "module", status: "active", ended_at: null, retrieval_degraded: false, play_allowed: true, sources: [] };
let container: HTMLDivElement;
let root: Root;
beforeEach(() => { vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true); vi.clearAllMocks(); localStorage.clear(); harness.busy = false; harness.historyReady = true; harness.announcement = ""; harness.turnOptions = null; harness.journal = { data: null, loading: false, error: null, stale: false, settledRevision: 0, refresh: vi.fn() }; container = document.createElement("div"); document.body.append(container); root = createRoot(container); });
afterEach(async () => { await act(() => root.unmount()); container.remove(); window.history.replaceState({}, "", "/"); vi.unstubAllGlobals(); });
it("preserves live transcript when readiness changes and disables module actions", async () => {
  const boot = { history: [{ role: "dm" as const, content: "Older boot history", created_at: "2026-10-14" }], campaignEnded: false };
  const game = { loading: false, error: null, title: "Lost module", campaignId: 2, campaign, campaignComplete: false, character: null, sceneId: null, combat: null, boot, applyTurnResult: vi.fn(), setCampaignComplete: vi.fn(), setSceneId: vi.fn(), reload: vi.fn(), updateCampaign: vi.fn() };
  const render = () => root.render(<MemoryRouter initialEntries={["/play/1"]}><Routes><Route path="/play/:sessionId" element={<Play />} /></Routes></MemoryRouter>);
  harness.game = game; await act(render);
  expect(harness.dispatch.mock.calls.filter(([action]) => action.type === "reset")).toHaveLength(1);
  harness.game = { ...game, campaign: { ...campaign, retrieval_degraded: true, play_allowed: false, sources: [{ slug: "lost_module", title: "Lost module", status: "missing", usable_chunks: 0, expected_chunks: null, collection_exists: false, source_pdf_available: false, message: "Missing source" }] } };
  await act(render);
  expect(container.textContent).toContain("Adventure sources need recovery"); expect(container.textContent).toContain("A recently saved turn"); expect(container.querySelector("textarea")?.disabled).toBe(true);
  expect(harness.dispatch.mock.calls.filter(([action]) => action.type === "reset")).toHaveLength(1);
  harness.game = { ...game, campaign: { ...campaign, source_mode: "homebrew", retrieval_degraded: true } };
  await act(render);
  expect(container.querySelector("textarea")?.disabled).toBe(false); expect(harness.dispatch.mock.calls.filter(([action]) => action.type === "reset")).toHaveLength(1);
});

function gameFixture() {
  const boot = { history: [{ role: "dm" as const, content: "Older boot history", created_at: "2026-10-14" }], campaignEnded: false };
  return { loading: false, error: null, title: "Lost module", campaignId: 2, campaign, campaignComplete: false, character: null, sceneId: null, combat: null, boot, applyTurnResult: vi.fn(), setCampaignComplete: vi.fn(), setSceneId: vi.fn(), reload: vi.fn(), updateCampaign: vi.fn() };
}

function journalFixture(): JournalRead {
  const clue = { id: "clue-1", category: "clue" as const, title: "Red seal", text: "A wax seal bears a star.", status: "discovered" as const, quantity: null, origin: { turn_number: 2, history_event_id: 41, place: null, actor: null, citation: null }, history: [{ status: "discovered" as const, turn_number: 2, history_event_id: 41 }], related_entry_id: null };
  return { session_id: 1, tracking: "available", entries: [clue], current_place: null, active_goal: null };
}

it("preserves journal filters across tool switches and keeps tools available during a slow turn", async () => {
  harness.game = gameFixture();
  harness.journal = { ...harness.journal, data: journalFixture() };
  harness.busy = true;
  const render = () => root.render(<MemoryRouter initialEntries={["/play/1"]}><Routes><Route path="/play/:sessionId" element={<Play />} /></Routes></MemoryRouter>);
  await act(render);
  const click = async (button: HTMLButtonElement | null) => act(() => button?.click());
  const button = (name: string) => Array.from(container.querySelectorAll("button")).find((item) => item.textContent === name) ?? null;
  await click(button("Journal"));
  expect(container.querySelector('[aria-label="Journal tool"]')).not.toBeNull();
  const search = container.querySelector<HTMLInputElement>('input[type="search"]')!;
  const setInput = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  await act(() => { setInput?.call(search, "seal"); search.dispatchEvent(new Event("input", { bubbles: true })); });
  const category = container.querySelector<HTMLSelectElement>("#journal-category")!;
  const setSelect = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
  await act(() => { setSelect?.call(category, "clue"); category.dispatchEvent(new Event("change", { bubbles: true })); });
  expect(container.querySelector<HTMLTextAreaElement>("textarea")?.disabled).toBe(false);
  await click(button("Close journal"));
  await click(button("Map"));
  expect(container.querySelector('[aria-label="Map tool"]')).not.toBeNull();
  await click(button("Close map"));
  await click(button("Journal"));
  expect(container.querySelector<HTMLInputElement>('input[type="search"]')?.value).toBe("seal");
  expect(container.querySelector<HTMLSelectElement>("#journal-category")?.value).toBe("clue");
});

it("opens verified journal history and reports only a newly committed journal delta", async () => {
  harness.game = gameFixture();
  harness.journal = { ...harness.journal, data: journalFixture() };
  const render = () => root.render(<MemoryRouter initialEntries={["/play/1"]}><Routes><Route path="/play/:sessionId" element={<Play />} /></Routes></MemoryRouter>);
  await act(render);
  const button = (name: string) => Array.from(container.querySelectorAll("button")).find((item) => item.textContent === name) ?? null;
  await act(() => button("Journal")?.click());
  const selectEntry = Array.from(container.querySelectorAll("button")).find((item) => item.textContent === "Select");
  await act(() => selectEntry?.click());
  expect(container.querySelector("#journal-entry-clue-1")?.getAttribute("aria-current")).toBe("true");
  await act(() => button("Journal")?.click());
  await act(() => button("Journal")?.click());
  await act(() => Array.from(container.querySelectorAll("button")).find((item) => item.textContent === "View narration")?.click());
  expect(container.querySelector("[data-open-event-id]")?.getAttribute("data-open-event-id")).toBe("41");

  const options = harness.turnOptions!;
  await act(() => options.onAcknowledged?.(1, "historical-receipt"));
  expect(harness.journal.refresh).not.toHaveBeenCalled();
  await act(() => options.onResult?.({ narration: "A new clue is recorded." }));
  expect(harness.journal.refresh).toHaveBeenCalledOnce();
  const updated = journalFixture();
  updated.entries.push({ ...updated.entries[0], id: "clue-2", title: "Moon mark", history: [], origin: { ...updated.entries[0].origin, turn_number: 3 } });
  harness.journal = { ...harness.journal, data: updated, settledRevision: 1 };
  await act(render);
  expect(container.textContent).toContain("Journal updated: Moon mark.");
});

it("opens selected journal entries from safe links and supports expanded, larger reading", async () => {
  harness.game = gameFixture();
  harness.journal = { ...harness.journal, data: journalFixture() };
  const render = (entry = "clue-1") => root.render(
    <MemoryRouter initialEntries={[`/play/1?tool=journal&entry=${entry}`]}>
      <Routes><Route path="/play/:sessionId" element={<Play />} /></Routes>
    </MemoryRouter>,
  );
  await act(() => render());
  const panel = container.querySelector<HTMLElement>('[aria-label="Journal tool"]');
  expect(panel).not.toBeNull();
  expect(container.querySelector("#journal-entry-clue-1")?.getAttribute("aria-current")).toBe("true");

  const button = (name: string) => Array.from(container.querySelectorAll("button")).find((item) => item.textContent === name) ?? null;
  await act(() => button("Expand view")?.click());
  expect(panel?.className).toContain("lg:w-[min(55vw,52rem)]");
  await act(() => button("Larger text")?.click());
  expect(container.querySelector("#journal-entry-clue-1 .journal-entry-copy")?.className).toContain("text-base");
  expect(container.querySelector("#journal-entry-clue-1 .journal-entry-copy")?.textContent).toBe("A wax seal bears a star.");
});


it("waits for the boot transcript before reconciling a saved pending request", async () => {
  harness.game = gameFixture();
  harness.historyReady = false;
  const request = { requestId: "lost-done", input: "Wait", draftRevision: 1 };
  localStorage.setItem("edgar_play_draft:v1:1", JSON.stringify({ text: "Wait", revision: 1, pendingRequest: request }));
  const render = () => root.render(<MemoryRouter initialEntries={["/play/1"]}><Routes><Route path="/play/:sessionId" element={<Play />} /></Routes></MemoryRouter>);
  await act(render);
  expect(harness.recover).not.toHaveBeenCalled();
  harness.historyReady = true;
  await act(render);
  expect(harness.recover).toHaveBeenCalledExactlyOnceWith(request);
});

const sheetCharacter = { id: 3, name: "Eda", className: "Fighter", level: 3, hpCurrent: 9, hpMax: 12, ac: 15, abilities: [], conditions: [], inventory: [{ label: "Carried", items: ["Red seal"] }], hitDice: null, secondWind: null, spellSlots: null };

function stubViewport(narrow: boolean) {
  const listeners = new Set<() => void>();
  const query = { matches: narrow, addEventListener: (_: string, listener: () => void) => listeners.add(listener), removeEventListener: (_: string, listener: () => void) => listeners.delete(listener) };
  vi.stubGlobal("matchMedia", () => query);
  return (next: boolean) => { query.matches = next; listeners.forEach((listener) => listener()); };
}

async function frame() {
  await act(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
}

function key(target: Element, init: KeyboardEventInit) {
  const event = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
  act(() => { target.dispatchEvent(event); });
  return event;
}

async function renderPlayWithSheet() {
  harness.game = { ...gameFixture(), character: sheetCharacter };
  harness.journal = { ...harness.journal, data: journalFixture() };
  await act(() => root.render(<MemoryRouter initialEntries={["/play/1"]}><Routes><Route path="/play/:sessionId" element={<Play />} /></Routes></MemoryRouter>));
  return container.querySelector<HTMLButtonElement>('button[aria-controls="character-sheet"]')!;
}

it("makes the narrow character sheet a contained dialog and restores the trigger when it closes", async () => {
  stubViewport(true);
  const trigger = await renderPlayWithSheet();
  const setValue = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
  const composer = container.querySelector<HTMLTextAreaElement>("textarea")!;
  await act(() => { setValue?.call(composer, "I knock twice"); composer.dispatchEvent(new Event("input", { bubbles: true })); });
  const close = async (dismiss: () => void) => {
    await act(async () => dismiss());
    expect(container.querySelector("#character-sheet")).toBeNull();
    expect(document.activeElement).toBe(trigger);
    expect(container.querySelector("[inert]")).toBeNull();
    expect(container.querySelector("textarea")?.value).toBe("I knock twice");
  };

  trigger.focus();
  await act(() => trigger.click());
  await frame();
  const sheet = container.querySelector<HTMLElement>("#character-sheet")!;
  expect(sheet.getAttribute("role")).toBe("dialog");
  expect(sheet.getAttribute("aria-modal")).toBe("true");
  expect(sheet.getAttribute("aria-label")).toBe("Character sheet");
  expect(trigger.closest("[inert]")?.getAttribute("aria-hidden")).toBe("true");
  expect(container.querySelector("textarea")?.closest("[inert]")).not.toBeNull();
  const [closeButton, readButton] = Array.from(sheet.querySelectorAll<HTMLElement>("button"));
  expect(document.activeElement).toBe(closeButton);
  expect(readButton.getAttribute("aria-label")).toBe("Read Red seal");

  expect(key(closeButton, { key: "Tab" }).defaultPrevented).toBe(false);
  readButton.focus();
  expect(key(readButton, { key: "Tab" }).defaultPrevented).toBe(true);
  expect(document.activeElement).toBe(closeButton);
  expect(key(closeButton, { key: "Tab", shiftKey: true }).defaultPrevented).toBe(true);
  expect(document.activeElement).toBe(readButton);

  await close(() => key(readButton, { key: "Escape" }));
  trigger.focus();
  await act(() => trigger.click());
  await frame();
  await close(() => container.querySelector<HTMLButtonElement>('button[aria-label="Close character sheet"]')!.click());
  trigger.focus();
  await act(() => trigger.click());
  await frame();
  await close(() => container.querySelector<HTMLElement>("#character-sheet")!.previousElementSibling!.dispatchEvent(new MouseEvent("click", { bubbles: true })));
});

it("keeps the desktop character sheet nonmodal and follows a breakpoint change without stranding focus", async () => {
  const setNarrow = stubViewport(false);
  const trigger = await renderPlayWithSheet();
  trigger.focus();
  await act(() => trigger.click());
  await frame();
  const sheet = container.querySelector<HTMLElement>("#character-sheet")!;
  expect(sheet.getAttribute("role")).toBeNull();
  expect(sheet.getAttribute("aria-modal")).toBeNull();
  expect(container.querySelector("[inert]")).toBeNull();
  expect(document.activeElement).toBe(trigger);
  expect(key(trigger, { key: "Tab" }).defaultPrevented).toBe(false);

  await act(() => setNarrow(true));
  await frame();
  expect(sheet.getAttribute("role")).toBe("dialog");
  expect(trigger.closest("[inert]")).not.toBeNull();
  expect(sheet.contains(document.activeElement)).toBe(true);

  await act(() => setNarrow(false));
  await frame();
  expect(sheet.getAttribute("role")).toBeNull();
  expect(container.querySelector("[inert]")).toBeNull();
  expect(trigger.closest("[aria-hidden='true']")).toBeNull();
});

it("closes a desktop tool from inside it and returns focus, but leaves Escape to the composer and IME", async () => {
  stubViewport(false);
  await renderPlayWithSheet();
  const toolbar = (name: string) => Array.from(container.querySelectorAll<HTMLButtonElement>("nav button")).find((item) => item.textContent === name)!;
  const composer = container.querySelector<HTMLTextAreaElement>("textarea")!;
  const open = async () => { toolbar("Journal").focus(); await act(() => toolbar("Journal").click()); await frame(); };
  const panel = () => container.querySelector('[aria-label="Journal tool"]');

  await open();
  expect(panel()?.getAttribute("role")).toBeNull();
  expect(container.querySelector("[inert]")).toBeNull();
  expect(Array.from(panel()!.querySelectorAll("button")).find((item) => item.textContent === "Expand view")?.className).toContain("max-lg:hidden");
  composer.focus();
  key(composer, { key: "Escape" });
  expect(panel()).not.toBeNull();
  key(composer, { key: "Escape", isComposing: true });
  expect(panel()).not.toBeNull();

  const search = container.querySelector<HTMLInputElement>('input[type="search"]')!;
  search.focus();
  key(search, { key: "Escape", isComposing: true });
  expect(panel()).not.toBeNull();
  key(search, { key: "Escape" });
  expect(panel()).toBeNull();
  expect(document.activeElement).toBe(toolbar("Journal"));

  await open();
  await act(() => container.querySelector<HTMLButtonElement>('button[aria-label="Close journal"]')!.click());
  expect(panel()).toBeNull();
  expect(document.activeElement).toBe(toolbar("Journal"));
});

it("keeps the single polite announcement region outside the inert background while a tool is open", async () => {
  stubViewport(true);
  harness.announcement = "The door opens.";
  const trigger = await renderPlayWithSheet();
  await act(() => trigger.click());
  const regions = container.querySelectorAll('[aria-live="polite"]');
  expect(regions).toHaveLength(1);
  expect(regions[0].textContent).toBe("The door opens.");
  expect(regions[0].getAttribute("role")).toBe("status");
  expect(regions[0].getAttribute("aria-atomic")).toBe("true");
  expect(regions[0].closest("[inert], [aria-hidden='true']")).toBeNull();
  expect(container.querySelector("h1")?.textContent).toBe("Lost module");
  expect(container.querySelector('[role="group"][aria-label="Current adventure context"]')).not.toBeNull();
});
