// @vitest-environment jsdom
/**
 * Mounted route lifecycle: real Play/Home routes, real hooks, fake HTTP transport. These check
 * what happens to in-flight requests and timers on unmount and route change, and that the
 * campaign/save navigation a player sees matches the server state.
 */
import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { AdventureRead, CampaignRead, ReadinessRead, SessionRead } from "../api/types";
import { ModeProvider } from "../dev/ModeProvider";
import { HOLD, installFakeApi, json, type FakeCall, type Responder } from "../test/fakeApi";
import Home from "./Home";
import Play from "./Play";

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  localStorage.clear();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  localStorage.clear();
});

function flush(ms = 0) {
  return act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms));
  });
}


const ready: ReadinessRead = { play_allowed: true, history_allowed: true, state: "ready", primary_action: "play", blockers: [] };

function campaign(overrides: Partial<CampaignRead> & Pick<CampaignRead, "id" | "title">): CampaignRead {
  return {
    system: "5e", created_by: 1, created_at: "2026-10-14", adventure_collections: ["lost_module"],
    source_mode: "module", status: "active", ended_at: null, retrieval_degraded: false,
    play_allowed: true, sources: [], readiness: ready, ...overrides,
  };
}

function session(overrides: Partial<SessionRead> & Pick<SessionRead, "id" | "campaign_id">): SessionRead {
  return {
    started_at: "2026-10-14", ended_at: null, current_scene_id: null, active_character_id: null,
    readiness: ready, ...overrides,
  };
}

const noCombat: Responder = () => json({ detail: { code: "not_found" } }, 404);

function playRoutes(): ReactElement {
  return <Routes><Route path="/play/:sessionId" element={<Play />} /></Routes>;
}

function renderPlay(entry = "/play/1") {
  return act(() => root.render(<ModeProvider><MemoryRouter initialEntries={[entry]}>{playRoutes()}</MemoryRouter></ModeProvider>));
}

it("unmounting the play route aborts every boot request still in flight", async () => {
  const { calls } = installFakeApi([
    [/^GET \/api\/sessions\/1$/, () => json(session({ id: 1, campaign_id: 2 }))],
    [/^GET \/api\/sessions\/1\/combat$/, noCombat],
  ]);
  await renderPlay();
  await flush();

  const held = calls.filter((c) => c.held);
  expect(held.length).toBeGreaterThanOrEqual(2);
  expect(held.every((c) => c.signal?.aborted === false)).toBe(true);

  await act(() => root.unmount());
  expect(held.map((c) => c.path)).toContain("/api/campaigns/2");
  expect(held.every((c) => c.signal?.aborted === true)).toBe(true);
});

it("changing sessions in place aborts the first boot and never shows its late response", async () => {
  let releaseFirst!: (response: Response) => void;
  const firstSession = new Promise<Response>((resolve) => { releaseFirst = resolve; });
  const { calls } = installFakeApi([
    [/^GET \/api\/sessions\/1$/, () => firstSession],
    [/^GET \/api\/sessions\/2$/, () => json(session({ id: 2, campaign_id: 3 }))],
    [/^GET \/api\/campaigns\/3$/, () => json(campaign({ id: 3, title: "Harbour saga" }))],
    [/^GET \/api\/sessions\/2\/messages$/, () => json([])],
    [/^GET \/api\/sessions\/2\/combat$/, noCombat],
    [/^GET \/api\/campaigns\/2$/, () => json(campaign({ id: 2, title: "Lost module" }))],
    [/^GET \/api\/sessions\/1\/messages$/, () => json([])],
    [/^GET \/api\/sessions\/1\/combat$/, noCombat],
  ]);

  function Shell() {
    const navigate = useNavigate();
    return (
      <>
        <button onClick={() => navigate("/play/2")}>Open session two</button>
        {playRoutes()}
      </>
    );
  }
  await act(() => root.render(<ModeProvider><MemoryRouter initialEntries={["/play/1"]}><Shell /></MemoryRouter></ModeProvider>));
  await flush();
  expect(container.textContent).not.toContain("Harbour saga");

  await act(() => Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "Open session two")!.click());
  await flush();
  expect(container.textContent).toContain("Harbour saga");

  const sessionOne = calls.find((c) => c.path === "/api/sessions/1")!;
  expect(sessionOne.signal?.aborted).toBe(true);

  // A transport that ignores the abort delivers the old save late; it must not replace session two.
  // Keep the act scope open while the late chain finishes, so its state updates are flushed here.
  await act(async () => {
    releaseFirst(json(session({ id: 1, campaign_id: 2 })));
    for (let i = 0; i < 25; i += 1) await new Promise((resolve) => setTimeout(resolve, 5));
  });
  expect(container.textContent).toContain("Harbour saga");
  expect(container.textContent).not.toContain("Lost module");
});

it("does not re-issue a pending save check after remount while the old one is aborted", async () => {
  localStorage.setItem("edgar_play_draft:v1:1", JSON.stringify({
    text: "Wait", revision: 1, pendingRequest: { requestId: "lost-done", input: "Wait", draftRevision: 1 },
  }));
  const receiptPath = /^GET \/api\/sessions\/1\/turns\/lost-done$/;
  const { callsTo } = installFakeApi([
    [/^GET \/api\/sessions\/1$/, () => json(session({ id: 1, campaign_id: 2 }))],
    [/^GET \/api\/campaigns\/2$/, () => json(campaign({ id: 2, title: "Lost module" }))],
    [/^GET \/api\/sessions\/1\/messages$/, () => json([])],
    [/^GET \/api\/sessions\/1\/combat$/, noCombat],
  ]);
  await renderPlay();
  await flush();
  const firstChecks = callsTo(receiptPath);
  expect(firstChecks).toHaveLength(1);

  await act(() => root.unmount());
  expect(firstChecks[0].signal?.aborted).toBe(true);

  root = createRoot(container);
  await renderPlay();
  await flush();
  const allChecks = callsTo(receiptPath);
  expect(allChecks).toHaveLength(2);
  const liveChecks = allChecks.filter((c) => c.held && c.signal && !c.signal.aborted);
  expect(liveChecks).toHaveLength(1);
});

// ── Home ─────────────────────────────────────────────────────────────────────────────────────

const user = { id: 1, email: "player@example.test", display_name: "Player", created_at: "2026-10-01" };

function adventure(overrides: Partial<AdventureRead> & Pick<AdventureRead, "slug" | "title">): AdventureRead {
  return { description: null, level_range: null, cover_image: null, status: "ready", job: null, ...overrides } as AdventureRead;
}

function homeRoutes() {
  return (
    <Routes>
      <Route path="/campaigns" element={<Home />} />
      <Route path="/campaigns/:id" element={<p>Campaign page</p>} />
      <Route path="/campaigns/:id/setup" element={<p>Setup page</p>} />
      <Route path="/play/:sessionId" element={<p>Play page</p>} />
    </Routes>
  );
}

function renderHome() {
  return act(() => root.render(<ModeProvider><MemoryRouter initialEntries={["/campaigns"]}>{homeRoutes()}</MemoryRouter></ModeProvider>));
}

function buttonWith(text: string, exact = false): HTMLButtonElement | undefined {
  return Array.from(container.querySelectorAll("button")).find((b) =>
    exact ? b.textContent?.trim() === text : (b.textContent ?? "").includes(text),
  );
}

it("polls only while a module is preparing, and stops the poll on unmount", async () => {
  vi.useFakeTimers();
  const processing = adventure({ slug: "lighthouse", title: "Lighthouse", status: "processing" });
  let adventureReads = 0;
  const { callsTo } = installFakeApi([
    [/^GET \/api\/users\/me$/, () => json(user)],
    // The first read is answered; every later poll stays in flight until aborted.
    [/^GET \/api\/adventures$/, () => { adventureReads += 1; return adventureReads === 1 ? json([processing]) : HOLD; }],
    [/^GET \/api\/campaigns$/, () => json([])],
    [/^GET \/api\/sessions$/, () => json([])],
  ]);
  await renderHome();
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(adventureReads).toBe(1);

  await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
  expect(callsTo(/^GET \/api\/adventures$/)).toHaveLength(2);
  const inFlight = callsTo(/^GET \/api\/adventures$/)[1];
  expect(inFlight.held).toBe(true);

  await act(() => root.unmount());
  expect(inFlight.signal?.aborted).toBe(true);
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(callsTo(/^GET \/api\/adventures$/)).toHaveLength(2);
});

it("stops polling once every module is ready", async () => {
  vi.useFakeTimers();
  const processing = adventure({ slug: "lighthouse", title: "Lighthouse", status: "processing" });
  const done = { ...processing, status: "ready" as const };
  let reads = 0;
  const { callsTo } = installFakeApi([
    [/^GET \/api\/users\/me$/, () => json(user)],
    [/^GET \/api\/adventures$/, () => json([reads++ === 0 ? processing : done])],
    [/^GET \/api\/campaigns$/, () => json([])],
    [/^GET \/api\/sessions$/, () => json([])],
  ]);
  await renderHome();
  await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
  expect(callsTo(/^GET \/api\/adventures$/)).toHaveLength(2);
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(callsTo(/^GET \/api\/adventures$/)).toHaveLength(2);
});

it("Resume opens a completed save as a read-only story, never as a playable session", async () => {
  const lost = adventure({ slug: "lost_module", title: "Lost module" });
  installFakeApi([
    [/^GET \/api\/users\/me$/, () => json(user)],
    [/^GET \/api\/adventures$/, () => json([lost])],
    [/^GET \/api\/campaigns$/, () => json([campaign({
      id: 5, title: "Lost module", status: "ended", ended_at: "2026-10-15", play_allowed: false,
      readiness: { ...ready, play_allowed: false, state: "completed", primary_action: "read_history" },
    })])],
    [/^GET \/api\/sessions$/, () => json([{
      ...session({ id: 9, campaign_id: 5, character_name: "Eda", has_history: true, last_played_at: "2026-10-15" }),
      readiness: { play_allowed: false, history_allowed: true, state: "completed", primary_action: "read_history", blockers: [] },
    }])],
  ]);
  await renderHome();
  await flush();
  const resume = buttonWith("Read story: Lost module");
  expect(resume).toBeDefined();
  expect(container.textContent).toContain("Eda · Save #9");
  await act(() => resume!.click());
  await flush();
  expect(container.textContent).toContain("Play page");
});

it("opens an ended campaign on its campaign page instead of starting a session", async () => {
  const lost = adventure({ slug: "lost_module", title: "Lost module" });
  const { calls } = installFakeApi([
    [/^GET \/api\/users\/me$/, () => json(user)],
    [/^GET \/api\/adventures$/, () => json([lost])],
    // Sources are ready, so an ended campaign keeps play_allowed true; readiness says it is complete.
    [/^GET \/api\/campaigns$/, () => json([campaign({ id: 5, title: "Lost module", status: "ended", ended_at: "2026-10-15",
      readiness: { ...ready, play_allowed: false, state: "completed", primary_action: "read_history" } })])],
    [/^GET \/api\/sessions$/, () => json([])],
  ]);
  await renderHome();
  await flush();
  const open = buttonWith("Open", true);
  expect(open).toBeDefined();
  await act(() => open!.click());
  await flush();
  expect(container.textContent).toContain("Campaign page");
  expect(calls.some((c: FakeCall) => c.method === "POST")).toBe(false);
});

it("keeps the typed generate form after a failed attempt and submits one more request on retry", async () => {
  const posts: FakeCall[] = [];
  let attempt = 0;
  installFakeApi([
    [/^GET \/api\/users\/me$/, () => json(user)],
    [/^GET \/api\/adventures$/, () => json([])],
    [/^GET \/api\/campaigns$/, () => json([])],
    [/^GET \/api\/sessions$/, () => json([])],
    [/^GET \/api\/characters$/, () => json([{ id: 3, name: "Eda", level: 3, character_class: "Fighter", current_assignment: null }])],
    [/^POST \/api\/adventures\/generate$/, (call) => {
      posts.push(call);
      attempt += 1;
      return attempt === 1 ? json({ code: "internal_fault" }, 500) : json({ slug: "lighthouse", title: "The Lighthouse of Whispers", status: "processing" }, 202);
    }],
  ]);
  await renderHome();
  await flush();

  const title = container.querySelector<HTMLInputElement>('input[placeholder="The Lighthouse of Whispers"]')!;
  const setValue = (el: HTMLInputElement | HTMLSelectElement, value: string) => {
    const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  };
  const hero = container.querySelector<HTMLSelectElement>("select")!;
  const budget = container.querySelector<HTMLInputElement>('input[type="number"][min="0.01"]')!;
  await act(async () => { setValue(hero, "3"); });
  await act(async () => { setValue(budget, "2"); });
  await act(async () => { setValue(title, "The Lighthouse of Whispers"); });
  await flush();
  const submit = () => buttonWith("Generate campaign", true)!;
  expect(submit().disabled).toBe(false);

  await act(() => submit().click());
  await flush();
  expect(posts).toHaveLength(1);
  expect(container.textContent).toContain("Something went wrong");
  expect(container.querySelector<HTMLInputElement>('input[placeholder="The Lighthouse of Whispers"]')?.value).toBe("The Lighthouse of Whispers");
  expect(submit().disabled).toBe(false);

  await act(() => submit().click());
  await flush();
  expect(posts).toHaveLength(2);
  expect(container.querySelector("#new-campaign")).toBeNull();
  expect(container.textContent).toContain("The Lighthouse of Whispers");
});

it.fails("a retried generate request carries the same identity as the failed attempt (known gap: no idempotency key)", async () => {
  // Documented gap: POST /api/adventures/generate has no request identity, so a retry after an
  // ambiguous failure can start a second module. Fixing it needs backend idempotency, so this
  // stays an expected failure until that contract exists.
  const posts: FakeCall[] = [];
  let attempt = 0;
  installFakeApi([
    [/^GET \/api\/users\/me$/, () => json(user)],
    [/^GET \/api\/adventures$/, () => json([])],
    [/^GET \/api\/campaigns$/, () => json([])],
    [/^GET \/api\/sessions$/, () => json([])],
    [/^GET \/api\/characters$/, () => json([{ id: 3, name: "Eda", level: 3, character_class: "Fighter", current_assignment: null }])],
    [/^POST \/api\/adventures\/generate$/, (call) => {
      posts.push(call);
      attempt += 1;
      return attempt === 1 ? json({ code: "internal_fault" }, 500) : json({ slug: "lighthouse", title: "Lighthouse", status: "processing" }, 202);
    }],
  ]);
  await renderHome();
  await flush();
  const setValue = (el: HTMLInputElement | HTMLSelectElement, value: string) => {
    const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  };
  await act(async () => { setValue(container.querySelector<HTMLSelectElement>("select")!, "3"); });
  await act(async () => { setValue(container.querySelector<HTMLInputElement>('input[type="number"][min="0.01"]')!, "2"); });
  await flush();
  const submit = () => buttonWith("Generate campaign", true)!;
  await act(() => submit().click());
  await flush();
  await act(() => submit().click());
  await flush();
  expect(posts).toHaveLength(2);
  expect(typeof posts[0].body).toBe("object");
  expect(typeof (posts[0].body as { request_id?: unknown }).request_id).toBe("string");
  expect((posts[1].body as { request_id?: unknown }).request_id).toBe((posts[0].body as { request_id?: unknown }).request_id);
});

it("unmounting while a readiness refresh is in flight aborts that refresh", async () => {
  const { usePlaySession } = await import("../components/play/usePlaySession");
  function ReadinessProbe() {
    const game = usePlaySession(1);
    return (
      <>
        <button onClick={() => void game.refreshReadiness()}>Refresh readiness</button>
        <p>{game.readiness?.state ?? "none"}</p>
      </>
    );
  }
  let sessionReads = 0;
  const { calls } = installFakeApi([
    // The boot read is answered; the refresh that follows stays in flight until aborted.
    [/^GET \/api\/sessions\/1$/, () => (++sessionReads === 1 ? json(session({ id: 1, campaign_id: 2 })) : HOLD)],
    [/^GET \/api\/campaigns\/2$/, () => json(campaign({ id: 2, title: "Lost module" }))],
    [/^GET \/api\/sessions\/1\/messages$/, () => json([])],
    [/^GET \/api\/sessions\/1\/combat$/, noCombat],
  ]);
  await act(() => root.render(<ModeProvider><MemoryRouter initialEntries={["/"]}><ReadinessProbe /></MemoryRouter></ModeProvider>));
  await flush();
  await act(() => Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "Refresh readiness")!.click());
  await flush();
  const refreshes = calls.filter((c) => c.path === "/api/sessions/1" && c.held);
  expect(refreshes).toHaveLength(1);
  await act(() => root.unmount());
  expect(refreshes[0].signal?.aborted).toBe(true);
});

it("renders only the public chat fields of a saved message, never extra planner-only fields", async () => {
  // Render guard for the mounted screen. The server privacy contract itself is covered by backend tests.
  const SENTINEL = "planner-only-sentinel-7731";
  installFakeApi([
    [/^GET \/api\/sessions\/1$/, () => json(session({ id: 1, campaign_id: 2 }))],
    [/^GET \/api\/campaigns\/2$/, () => json(campaign({ id: 2, title: "Lost module" }))],
    [/^GET \/api\/sessions\/1\/messages$/, () => json([{
      id: 1, role: "dm", content: "The bell tolls once.", created_at: "2026-10-14",
      planner_secret: SENTINEL, hidden_npc_name: SENTINEL,
    }])],
    [/^GET \/api\/sessions\/1\/combat$/, noCombat],
  ]);
  await renderPlay();
  await flush();
  expect(container.textContent).toContain("The bell tolls once.");
  expect(container.innerHTML).not.toContain(SENTINEL);
});

it("a mounted boot hook that changes session ignores the old session's late response", async () => {
  // Play keys its screen by session, so routes never reach this case; the hook must still
  // guard it, because a caller without that key would otherwise show the old save.
  const { usePlaySession } = await import("../components/play/usePlaySession");
  function BootProbe({ id }: { id: number }) {
    const game = usePlaySession(id);
    return <p>{game.loading ? "loading" : game.title}</p>;
  }
  let releaseFirst!: (response: Response) => void;
  const firstSession = new Promise<Response>((resolve) => { releaseFirst = resolve; });
  const { calls } = installFakeApi([
    [/^GET \/api\/sessions\/1$/, () => firstSession],
    [/^GET \/api\/sessions\/2$/, () => json(session({ id: 2, campaign_id: 3 }))],
    [/^GET \/api\/campaigns\/3$/, () => json(campaign({ id: 3, title: "Harbour saga" }))],
    [/^GET \/api\/campaigns\/2$/, () => json(campaign({ id: 2, title: "Lost module" }))],
    [/^GET \/api\/sessions\/[12]\/messages$/, () => json([])],
    [/^GET \/api\/sessions\/[12]\/combat$/, noCombat],
  ]);
  await act(() => root.render(<ModeProvider><MemoryRouter initialEntries={["/"]}><BootProbe id={1} /></MemoryRouter></ModeProvider>));
  await act(() => root.render(<ModeProvider><MemoryRouter initialEntries={["/"]}><BootProbe id={2} /></MemoryRouter></ModeProvider>));
  await flush();
  expect(container.textContent).toBe("Harbour saga");

  await act(async () => {
    releaseFirst(json(session({ id: 1, campaign_id: 2 })));
    for (let i = 0; i < 25; i += 1) await new Promise((resolve) => setTimeout(resolve, 5));
  });
  expect(calls.find((c) => c.path === "/api/sessions/1")?.signal?.aborted).toBe(true);
  expect(container.textContent).toBe("Harbour saga");
});
