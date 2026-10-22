// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { CampaignRead, ReadinessRead } from "../api/types";
import type { JournalRead } from "../components/journal/types";
import { lobbyFixture, spotlightFixture } from "../components/lobby/fixtures";
import { turnGate } from "../components/lobby/tableState";
import Play from "./Play";
const harness = vi.hoisted(() => ({
  game: null as unknown,
  dispatch: vi.fn(),
  send: vi.fn(),
  busy: false,
  historyReady: true,
  announcement: "",
  recover: vi.fn(),
  turnOptions: null as null | { onResult?: (result: unknown) => void; onAcknowledged?: (revision: number, requestId: string) => void; onReadinessRejected?: () => void },
  lobby: null as unknown as Record<string, unknown>,
  lobbyOptions: null as null | { onTurnFinished?: (info: { own: boolean; outcome: string }) => void },
  journal: { data: null as unknown, loading: false, error: null as unknown, stale: false, settledRevision: 0, settledRequestRevision: -1, refresh: vi.fn().mockReturnValue(1) },
}));
vi.mock("../dev/ModeContext", () => ({ useMode: () => ({ isDev: false }) }));
vi.mock("../components/play/usePlaySession", () => ({ usePlaySession: () => harness.game }));
vi.mock("../components/play/useTurn", () => ({ useTurn: (_sessionId: number, options: typeof harness.turnOptions) => { harness.turnOptions = options; return { dispatch: harness.dispatch, send: harness.send, stop: vi.fn(), retry: vi.fn(), recover: harness.recover, checkReceipt: vi.fn(), busy: harness.busy, state: { historyReady: harness.historyReady, lines: [{ id: "live", role: "dm", content: "A recently saved turn" }], pending: null, announcement: harness.announcement } }; } }));
vi.mock("../components/lobby/useSessionLobby", () => ({ useSessionLobby: (_id: number, options: typeof harness.lobbyOptions) => { harness.lobbyOptions = options; return harness.lobby; } }));
vi.mock("../components/journal/api", () => ({ useJournal: () => harness.journal }));
vi.mock("../components/MessageList", () => ({ MessageList: (props: { openEventId?: number | null }) => <p data-open-event-id={props.openEventId ?? "none"}>A recently saved turn</p> }));
const campaign: CampaignRead = { id: 2, title: "Lost module", system: "5e", created_by: 1, created_at: "2026-10-14", adventure_collections: ["lost_module"], source_mode: "module", status: "active", ended_at: null, retrieval_degraded: false, play_allowed: true, sources: [] };
const ready: ReadinessRead = { play_allowed: true, history_allowed: true, state: "ready", primary_action: "play", blockers: [] };
const blocked: ReadinessRead = { play_allowed: false, history_allowed: true, state: "source_missing", primary_action: "recover_source", blockers: [{ code: "source_missing", message: "The campaign source is missing or has no usable text. Recover the source to continue.", action: "recover_source" }] };
function lobbyState(viewer?: Parameters<typeof lobbyFixture>[0], spotlight = spotlightFixture(), over: Record<string, unknown> = {}) {
  const lobby = viewer ? lobbyFixture(viewer) : null;
  return { lobby, loaded: true, revoked: false, announcement: "", activity: null, spotlight, gate: turnGate(lobby?.viewer ?? null, viewer ? spotlight : null), ...over };
}
let container: HTMLDivElement;
let root: Root;
beforeEach(() => { vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true); vi.clearAllMocks(); localStorage.clear(); harness.busy = false; harness.historyReady = true; harness.announcement = ""; harness.turnOptions = null; harness.lobbyOptions = null; harness.lobby = lobbyState(); harness.journal = { data: null, loading: false, error: null, stale: false, settledRevision: 0, settledRequestRevision: -1, refresh: vi.fn().mockReturnValue(1) }; container = document.createElement("div"); document.body.append(container); root = createRoot(container); });
afterEach(async () => { await act(() => root.unmount()); container.remove(); window.history.replaceState({}, "", "/"); vi.unstubAllGlobals(); });
it("preserves live transcript when readiness changes and disables module actions", async () => {
  const boot = { history: [{ role: "dm" as const, content: "Older boot history", created_at: "2026-10-14" }], campaignEnded: false };
  const game = { loading: false, error: null, title: "Lost module", campaignId: 2, campaign, readiness: ready, refreshReadiness: vi.fn(), campaignComplete: false, character: null, sceneId: null, combat: null, boot, applyTurnResult: vi.fn(), setCampaignComplete: vi.fn(), setSceneId: vi.fn(), reload: vi.fn(), updateCampaign: vi.fn() };
  const render = () => root.render(<MemoryRouter initialEntries={["/play/1"]}><Routes><Route path="/play/:sessionId" element={<Play />} /></Routes></MemoryRouter>);
  harness.game = game; await act(render);
  expect(harness.dispatch.mock.calls.filter(([action]) => action.type === "reset")).toHaveLength(1);
  const draftInput = container.querySelector<HTMLTextAreaElement>("textarea")!;
  const setTextareaValue = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
  await act(() => {
    setTextareaValue?.call(draftInput, "I check the doorway before entering.");
    draftInput.dispatchEvent(new Event("input", { bubbles: true }));
  });
  harness.game = { ...game, readiness: blocked, campaign: { ...campaign, retrieval_degraded: true, play_allowed: false, sources: [{ slug: "lost_module", title: "Lost module", status: "missing", usable_chunks: 0, expected_chunks: null, collection_exists: false, source_pdf_available: false, message: "Missing source" }] } };
  await act(render);
  expect(container.textContent).toContain("Adventure sources need recovery"); expect(container.textContent).toContain("A recently saved turn"); expect(container.querySelector("textarea")?.disabled).toBe(true);
  expect(container.querySelector<HTMLTextAreaElement>("textarea")?.value).toBe("I check the doorway before entering.");
  const readinessNotices = Array.from(container.querySelectorAll("section")).filter((section) => section.textContent?.includes(blocked.blockers[0].message));
  expect(readinessNotices).toHaveLength(1);
  expect(readinessNotices.filter((section) => section.getAttribute("aria-live") === "polite")).toHaveLength(0);
  expect(Array.from(container.querySelectorAll('[role="status"][aria-live="polite"]')).filter((section) => section.textContent?.includes(blocked.blockers[0].message))).toHaveLength(1);
  expect(harness.dispatch.mock.calls.filter(([action]) => action.type === "reset")).toHaveLength(1);
  harness.game = { ...game, readiness: ready, campaign: { ...campaign, source_mode: "homebrew", retrieval_degraded: true } };
  await act(render);
  expect(container.querySelector("textarea")?.disabled).toBe(false); expect(harness.dispatch.mock.calls.filter(([action]) => action.type === "reset")).toHaveLength(1);
});

function gameFixture() {
  const boot = { history: [{ role: "dm" as const, content: "Older boot history", created_at: "2026-10-14" }], campaignEnded: false };
  return { loading: false, error: null, title: "Lost module", campaignId: 2, campaign, readiness: ready, refreshReadiness: vi.fn(), campaignComplete: false, character: null, sceneId: null, combat: null, boot, applyTurnResult: vi.fn(), setCampaignComplete: vi.fn(), setSceneId: vi.fn(), reload: vi.fn(), updateCampaign: vi.fn() };
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
  await click(container.querySelector<HTMLButtonElement>("#journal-tab-clues"));
  const search = container.querySelector<HTMLInputElement>('input[type="search"]')!;
  const setInput = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  await act(() => { setInput?.call(search, "seal"); search.dispatchEvent(new Event("input", { bubbles: true })); });

  expect(container.querySelector<HTMLTextAreaElement>("textarea")?.disabled).toBe(false);
  await click(button("Close journal"));
  await click(button("Map"));
  expect(container.querySelector('[aria-label="Map tool"]')).not.toBeNull();
  await click(button("Close map"));
  await click(button("Journal"));
  expect(container.querySelector<HTMLInputElement>('input[type="search"]')?.value).toBe("seal");
  expect(container.textContent).toContain("entry matches");
  expect(JSON.parse(localStorage.getItem("edgar_play_workspace:v1:1")!).view).toBe("clues");
});

it("opens verified journal history and reports only a newly committed journal delta", async () => {
  harness.game = gameFixture();
  harness.journal = { ...harness.journal, data: journalFixture() };
  const render = () => root.render(<MemoryRouter initialEntries={["/play/1"]}><Routes><Route path="/play/:sessionId" element={<Play />} /></Routes></MemoryRouter>);
  await act(render);
  const button = (name: string) => Array.from(container.querySelectorAll("button")).find((item) => item.textContent === name) ?? null;
  await act(() => button("Journal")?.click());
  const selectEntry = container.querySelector<HTMLButtonElement>('button[aria-label="Open clue: Red seal"]');
  await act(() => selectEntry?.click());
  expect(container.querySelector("#journal-entry-clue-1")?.getAttribute("aria-current")).toBe("true");
  await act(() => button("Journal")?.click());
  await act(() => button("Journal")?.click());
  await act(() => Array.from(container.querySelectorAll("button")).find((item) => item.textContent === "Read the moment")?.click());
  expect(container.querySelector("[data-open-event-id]")?.getAttribute("data-open-event-id")).toBe("41");

  const options = harness.turnOptions!;
  await act(() => options.onAcknowledged?.(1, "historical-receipt"));
  expect(harness.journal.refresh).not.toHaveBeenCalled();
  await act(() => options.onResult?.({ narration: "A new clue is recorded." }));
  expect(harness.journal.refresh).toHaveBeenCalledOnce();
  const updated = journalFixture();
  updated.entries.push({ ...updated.entries[0], id: "clue-2", title: "Moon mark", history: [], origin: { ...updated.entries[0].origin, turn_number: 3 } });
  harness.journal = { ...harness.journal, data: updated, settledRevision: 1, settledRequestRevision: 1 };
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

it("keeps very long campaign and hero names compact while exposing their full names", async () => {
  stubViewport(true);
  const campaignTitle = "C".repeat(150);
  const heroName = "長".repeat(255);
  const clueTitle = "The letter carried by the tide";
  harness.game = { ...gameFixture(), title: campaignTitle, character: { ...sheetCharacter, name: heroName } };
  const journal = journalFixture();
  journal.entries.push({ ...journal.entries[0], id: "clue-long", title: clueTitle, text: `“${"A quoted clue with a very long unbroken inscription ".repeat(8)}”` });
  harness.journal = { ...harness.journal, data: journal };
  await act(() => root.render(<MemoryRouter initialEntries={["/play/1"]}><Routes><Route path="/play/:sessionId" element={<Play />} /></Routes></MemoryRouter>));

  const title = container.querySelector<HTMLAnchorElement>("h1 a")!;
  expect(title.textContent).toBe(campaignTitle);
  expect(title.className).toContain("overflow-wrap:anywhere");
  expect(title.className).toContain("line-clamp-2");
  expect(title.hasAttribute("title")).toBe(false);
  const titleDisclosure = container.querySelector<HTMLDetailsElement>("header details")!;
  const titleSummary = titleDisclosure.querySelector<HTMLElement>("summary")!;
  expect(titleSummary.textContent).toBe("Full title");
  expect(titleSummary.className).toContain("min-h-11");
  await act(() => titleSummary.click());
  expect(titleDisclosure.open).toBe(true);
  const fullTitle = titleDisclosure.querySelector<HTMLElement>("div")!;
  expect(fullTitle.textContent).toBe(campaignTitle);
  expect(fullTitle.className).toContain("max-h-[min(35dvh,14rem)]");
  expect(fullTitle.className).toContain("overflow-y-auto");
  const heroButton = container.querySelector<HTMLButtonElement>('button[aria-controls="character-sheet"]')!;
  expect(heroButton.textContent).toContain(heroName);
  expect(heroButton.getAttribute("aria-label")).toContain(heroName);
  expect(heroButton.className).toContain("min-h-11");
  expect(heroButton.querySelector("span span")?.className).toContain("line-clamp-1");

  await act(() => heroButton.click());
  const sheet = container.querySelector<HTMLElement>("#character-sheet")!;
  expect(sheet.querySelector("h2")?.textContent).toBe(heroName);
  expect(sheet.querySelector("h2")?.className).toContain("overflow-wrap:anywhere");
  expect(sheet.querySelector<HTMLButtonElement>('button[aria-label="Close character sheet"]')?.className).toContain("min-w-11");
  await act(() => sheet.querySelector<HTMLButtonElement>('button[aria-label="Close character sheet"]')?.click());

  await act(() => Array.from(container.querySelectorAll<HTMLButtonElement>("nav button")).find((button) => button.textContent === "Journal")?.click());
  expect(container.querySelector('[aria-label="Journal tool"]')?.textContent).toContain(clueTitle);
  expect(container.querySelector("[aria-label='Current adventure context']")?.className).toContain("overflow-wrap:anywhere");
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

it("reflows journal and expanded map tools across breakpoints without losing the draft or return focus", async () => {
  const setNarrow = stubViewport(false);
  harness.game = gameFixture();
  harness.journal = { ...harness.journal, data: journalFixture() };
  await act(() => root.render(<MemoryRouter initialEntries={["/play/1"]}><Routes><Route path="/play/:sessionId" element={<Play />} /></Routes></MemoryRouter>));
  const toolbar = (name: string) => Array.from(container.querySelectorAll<HTMLButtonElement>("nav button")).find((item) => item.textContent === name)!;
  const composer = container.querySelector<HTMLTextAreaElement>("textarea")!;
  const setValue = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
  await act(() => { setValue?.call(composer, "Keep this draft through a viewport resize."); composer.dispatchEvent(new Event("input", { bubbles: true })); });

  toolbar("Journal").focus();
  await act(() => toolbar("Journal").click());
  const journalPanel = () => container.querySelector<HTMLElement>('[aria-label="Journal tool"]');
  expect(journalPanel()?.getAttribute("role")).toBeNull();
  expect(container.querySelector("[aria-label='Close adventure tool']")).toBeNull();
  await act(() => setNarrow(true));
  await frame();
  expect(journalPanel()?.getAttribute("role")).toBe("dialog");
  expect(journalPanel()?.getAttribute("aria-modal")).toBe("true");
  expect(container.querySelectorAll("[aria-label='Close adventure tool']")).toHaveLength(1);
  expect(container.querySelector("[inert]")).not.toBeNull();
  expect(composer.value).toBe("Keep this draft through a viewport resize.");

  await act(() => setNarrow(false));
  await frame();
  expect(journalPanel()?.getAttribute("role")).toBeNull();
  expect(container.querySelector("[inert]")).toBeNull();
  expect(container.querySelectorAll("[aria-label='Close adventure tool']")).toHaveLength(0);
  expect(composer.value).toBe("Keep this draft through a viewport resize.");
  const search = container.querySelector<HTMLInputElement>('input[type="search"]')!;
  search.focus();
  key(search, { key: "Escape" });
  expect(journalPanel()).toBeNull();
  expect(document.activeElement).toBe(toolbar("Journal"));

  toolbar("Map").focus();
  await act(() => toolbar("Map").click());
  const mapPanel = () => container.querySelector<HTMLElement>('[aria-label="Map tool"]');
  const fullView = Array.from(mapPanel()!.querySelectorAll<HTMLButtonElement>("button")).find((item) => item.textContent === "Full view")!;
  fullView.focus();
  await act(() => fullView.click());
  expect(mapPanel()?.getAttribute("aria-modal")).toBe("true");
  await act(() => setNarrow(true));
  await frame();
  expect(mapPanel()?.getAttribute("aria-modal")).toBe("true");
  expect(container.querySelector("[inert]")).not.toBeNull();
  await act(() => mapPanel()?.querySelector<HTMLButtonElement>('button[aria-label="Close map"]')?.click());
  expect(mapPanel()).toBeNull();
  expect(document.activeElement).toBe(toolbar("Map"));
  expect(composer.value).toBe("Keep this draft through a viewport resize.");
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

  await open();
  key(toolbar("Journal"), { key: "Escape" });
  expect(panel()).toBeNull();

  await open();
  toolbar("Map").focus();
  await act(() => toolbar("Map").click());
  const closeMap = container.querySelector<HTMLButtonElement>('[aria-label="Close map"]')!;
  closeMap.focus();
  key(closeMap, { key: "Escape" });
  expect(container.querySelector('[aria-label="Map tool"]')).toBeNull();
  expect(document.activeElement).toBe(toolbar("Map"));
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


// Multiplayer table (EDG-050): the play screen follows the spotlight.
function renderPlayScreen() {
  harness.game = { ...gameFixture(), refreshShared: vi.fn() };
  return act(() => root.render(<MemoryRouter initialEntries={["/play/1"]}><Routes><Route path="/play/:sessionId" element={<Play />} /></Routes></MemoryRouter>));
}

it("shows a waiting state with who is acting instead of a dead input for a player who does not hold the spotlight", async () => {
  harness.lobby = lobbyState({ user_id: 2, role: "player", character_id: 12, character_name: "Bran" }, spotlightFixture(), {
    activity: { actor: { user_id: 1, display_name: "Alice", character_id: 11, character_name: "Ayla" }, stage: "narrating" },
  });
  await renderPlayScreen();
  expect(container.querySelector("textarea")).toBeNull();
  const banner = container.querySelector('[data-testid="turn-banner"]')!;
  expect(banner.getAttribute("data-gate")).toBe("waiting");
  expect(banner.textContent).toContain("Waiting for Alice (Ayla)");
  expect(banner.textContent).toContain("Alice is narrating");
  expect(container.querySelector('[data-testid="read-only-composer"]')).not.toBeNull();
  expect(container.querySelector('a[href="/play/1/table"]')).not.toBeNull();
});

it("tells the spotlight holder it is their turn and keeps the action input", async () => {
  harness.lobby = lobbyState({ user_id: 1, role: "player", character_id: 11, character_name: "Ayla" });
  await renderPlayScreen();
  expect(container.querySelector('[data-testid="turn-banner"]')!.textContent).toContain("Your turn");
  expect(container.querySelector<HTMLTextAreaElement>("textarea")!.disabled).toBe(false);
});

it("gives a spectator a read-only play screen with no action input", async () => {
  harness.lobby = lobbyState({ user_id: 3, role: "spectator", character_id: null, character_name: null });
  await renderPlayScreen();
  expect(container.querySelector("textarea")).toBeNull();
  expect(container.querySelector('input[type="text"], input:not([type])')).toBeNull();
  expect(container.querySelector('[data-testid="turn-banner"]')!.textContent).toContain("You are watching");
  expect(container.querySelector('[data-testid="read-only-composer"]')!.textContent).toContain("Spectating");
  // The journal and the map stay reachable for a spectator.
  expect(Array.from(container.querySelectorAll("button")).some((b) => b.textContent === "Journal")).toBe(true);
  expect(Array.from(container.querySelectorAll("button")).some((b) => b.textContent === "Map")).toBe(true);
});

it("never starts the story for a spectator", async () => {
  const fetchSpy = vi.fn<(url: string) => Promise<Response>>(async () => new Response("{}", { status: 200 }));
  vi.stubGlobal("fetch", fetchSpy);
  harness.lobby = lobbyState({ user_id: 3, role: "spectator", character_id: null, character_name: null });
  harness.game = { ...gameFixture(), boot: { history: [], campaignEnded: false }, refreshShared: vi.fn() };
  await act(() => root.render(<MemoryRouter initialEntries={["/play/1"]}><Routes><Route path="/play/:sessionId" element={<Play />} /></Routes></MemoryRouter>));
  expect(fetchSpy.mock.calls.some(([url]) => String(url).includes("/intro"))).toBe(false);
});

it("announces table changes in the one polite region", async () => {
  harness.lobby = lobbyState({ user_id: 2, role: "player", character_id: 12, character_name: "Bran" }, spotlightFixture(), { announcement: "It is now Ayla's turn, played by Alice." });
  await renderPlayScreen();
  const regions = container.querySelectorAll('[aria-live="polite"]');
  expect(regions).toHaveLength(1);
  expect(regions[0].textContent).toBe("It is now Ayla's turn, played by Alice.");
});

it("re-reads shared state when another member's turn commits, but not for the viewer's own or a refused one", async () => {
  harness.lobby = lobbyState({ user_id: 2, role: "player", character_id: 12, character_name: "Bran" });
  await renderPlayScreen();
  const refreshShared = (harness.game as { refreshShared: ReturnType<typeof vi.fn> }).refreshShared;
  await act(() => harness.lobbyOptions!.onTurnFinished!({ own: true, outcome: "committed" }));
  await act(() => harness.lobbyOptions!.onTurnFinished!({ own: false, outcome: "not_committed" }));
  expect(refreshShared).not.toHaveBeenCalled();
  await act(() => harness.lobbyOptions!.onTurnFinished!({ own: false, outcome: "committed" }));
  expect(refreshShared).toHaveBeenCalledTimes(1);
  expect(harness.journal.refresh).toHaveBeenCalled();
});

it("defers the shared re-read while the viewer's own turn is running", async () => {
  harness.lobby = lobbyState({ user_id: 1, role: "player", character_id: 11, character_name: "Ayla" });
  harness.busy = true;
  await renderPlayScreen();
  const refreshShared = (harness.game as { refreshShared: ReturnType<typeof vi.fn> }).refreshShared;
  await act(() => harness.lobbyOptions!.onTurnFinished!({ own: false, outcome: "committed" }));
  expect(refreshShared).not.toHaveBeenCalled();
  harness.busy = false;
  await act(() => root.render(<MemoryRouter initialEntries={["/play/1"]}><Routes><Route path="/play/:sessionId" element={<Play />} /></Routes></MemoryRouter>));
  expect(refreshShared).toHaveBeenCalledTimes(1);
});

it("shows an access-ended screen once the membership is revoked", async () => {
  harness.lobby = lobbyState({ user_id: 2, role: "player", character_id: 12, character_name: "Bran" }, spotlightFixture(), { revoked: true });
  await renderPlayScreen();
  expect(container.textContent).toContain("You no longer have access to this table");
  expect(container.querySelector("textarea")).toBeNull();
});

it("shows the earned lead in the header and recap and opens it in the journal", async () => {
  const data = journalFixture();
  const lead = { ...data.entries[0], id: "earned-answer", category: "note" as const, status: "learned" as const,
    title: "Follow the red seal", text: "The red seal belongs to the harbor keeper." };
  harness.journal = { ...harness.journal, data: { ...data, entries: [...data.entries, lead], current_lead: lead } };
  harness.game = gameFixture();
  await act(() => root.render(<MemoryRouter initialEntries={["/play/1"]}><Routes><Route path="/play/:sessionId" element={<Play />} /></Routes></MemoryRouter>));
  const context = container.querySelector('[aria-label="Current adventure context"]')!;
  expect(context.textContent).toContain("Latest lead: Follow the red seal");
  expect(container.textContent).not.toContain("No active goal recorded");
  await act(() => context.querySelector<HTMLButtonElement>("button")?.click());
  expect(container.querySelector("#journal-entry-earned-answer")?.getAttribute("aria-current")).toBe("true");
  expect(container.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toContain("Clues");
});

it("migrates a saved journal category into its matching section", async () => {
  localStorage.setItem("edgar_play_workspace:v1:1", JSON.stringify({
    activeTool: "journal", category: "promise", query: "", selectedEntryId: null,
  }));
  harness.journal = { ...harness.journal, data: journalFixture() };
  harness.game = gameFixture();
  await act(() => root.render(<MemoryRouter initialEntries={["/play/1"]}><Routes><Route path="/play/:sessionId" element={<Play />} /></Routes></MemoryRouter>));
  expect(container.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toBe("Goals");
  expect(JSON.parse(localStorage.getItem("edgar_play_workspace:v1:1")!).view).toBe("goals");
});

it("keeps desktop consultation optional, confirms beside the composer, and appends inspected targets without sending", async () => {
  stubViewport(false);
  harness.game = { ...gameFixture(), combat: { round: 2, ended: false, outcome: null, pending_declaration: { summary: "Disengage, then withdraw. No dice rolled yet." }, combatants: [
    { id: "hero:1", name: "Mira", is_player: true, hp_current: 11, hp_max: 44, alive: true },
    { id: "actor:guz", name: "Guz", is_player: false, hp_current: 67, hp_max: 67, alive: true, band: "melee" },
  ] } };
  harness.journal = { ...harness.journal, data: journalFixture() };
  await act(() => root.render(<MemoryRouter initialEntries={["/play/1"]}><Routes><Route path="/play/:sessionId" element={<Play />} /></Routes></MemoryRouter>));
  const textarea = container.querySelector<HTMLTextAreaElement>("textarea")!;
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
  await act(() => { setter.call(textarea, "I ask"); textarea.dispatchEvent(new Event("input", { bubbles: true })); });
  expect(container.querySelector('[aria-label="Adventure orientation and tools"]')?.textContent).not.toContain("Session recap");
  const confirmation = container.querySelector('[aria-label="Confirm declaration"]')!;
  expect(confirmation.closest("main")?.contains(textarea)).toBe(true);
  expect(confirmation.closest('[aria-label="Combat"]')).toBeNull();
  const encounterButton = Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "Encounter view")!;
  await act(() => encounterButton.click());
  expect(container.querySelector('[aria-label="Encounter schematic"]')).not.toBeNull();
  await act(() => container.querySelector<HTMLButtonElement>('[aria-label="Inspect Guz"]')!.click());
  expect(textarea.value).toBe("I ask");
  await act(() => Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "Full view")!.click());
  await frame();
  expect(container.querySelector('[aria-label="Map tool"]')?.getAttribute("aria-modal")).toBe("true");
  await act(() => Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "Add target to draft")!.click());
  await frame();
  expect(textarea.value).toBe("I ask Guz");
  expect(container.querySelector('[aria-label="Map tool"]')?.getAttribute("aria-modal")).toBeNull();
  expect(document.activeElement).toBe(textarea);
  expect(harness.send).not.toHaveBeenCalled();
  await act(() => container.querySelector<HTMLButtonElement>('[data-play-tool-trigger="recap"]')!.click());
  expect(container.querySelector('aside[aria-label^="Recap"]')?.textContent).toContain("Session recap");
  expect(container.querySelector('aside[aria-label^="Recap"]')?.textContent).toContain("Ideas");
  expect(textarea.value).toBe("I ask Guz");
});
