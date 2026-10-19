// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import * as api from "../../api/client";
import type { ReadinessRead, SessionRead } from "../../api/types";
import { usePlaySession } from "./usePlaySession";

vi.mock("../../api/client", () => ({
  getSession: vi.fn(), getCampaign: vi.fn(), getCharacter: vi.fn(), getSessionCombat: vi.fn(),
  listCharacterAssignments: vi.fn(), listSessionMessages: vi.fn(), getAdventure: vi.fn(),
}));
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
const ready: ReadinessRead = { play_allowed: true, history_allowed: true, state: "ready", primary_action: "play", blockers: [] };
const blocked: ReadinessRead = { play_allowed: false, history_allowed: true, state: "adventure_review_needed", primary_action: "review_adventure", blockers: [{code:"adventure_review_needed",message:"Review first",action:"review_adventure"}] };
const session = (id: number, readiness: ReadinessRead = ready) => ({id, campaign_id:id, readiness, active_character_id:null, current_scene_id:null}) as SessionRead;
let dispose: (() => Promise<void>) | undefined;
afterEach(async () => { await dispose?.(); dispose = undefined; vi.resetAllMocks(); vi.unstubAllGlobals(); });
async function mount(id = 1) {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.mocked(api.getCampaign).mockImplementation(async (cid) => ({id:cid,title:`Campaign ${cid}`,system:"5e",created_by:1,created_at:"2026-10-16T09:00:00Z",ended_at:null,adventure_collections:[],status:"active"}));
  vi.mocked(api.getSessionCombat).mockResolvedValue(null);
  vi.mocked(api.listSessionMessages).mockResolvedValue([{role:"dm",content:"Exact saved quotation."}] as Awaited<ReturnType<typeof api.listSessionMessages>>);
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  let hook!: ReturnType<typeof usePlaySession>;
  function Harness({sessionId}:{sessionId:number}) { hook=usePlaySession(sessionId); return <p>{hook.title}</p>; }
  dispose=async()=>{await act(()=>root.unmount());container.remove();};
  await act(()=>root.render(<Harness sessionId={id} />));
  return { get hook(){return hook;}, render:async (sessionId:number)=>{ await act(()=>root.render(<Harness sessionId={sessionId} />)); } };
}

it("keeps a newer readiness result while an older boot fetch finishes", async () => {
  const boot=deferred<SessionRead>();
  vi.mocked(api.getSession).mockReturnValueOnce(boot.promise).mockResolvedValueOnce(session(1,blocked));
  const view=await mount();
  await act(()=>view.hook.refreshReadiness());
  await act(async()=>boot.resolve(session(1)));
  expect(view.hook.loading).toBe(false);
  expect(view.hook.readiness).toEqual(blocked);
  expect(view.hook.boot?.history[0].content).toBe("Exact saved quotation.");
});

it("ignores late refreshes from another session and older requests", async () => {
  vi.mocked(api.getSession).mockResolvedValue(session(1));
  const view=await mount();
  const old=deferred<SessionRead>();
  vi.mocked(api.getSession).mockReturnValueOnce(old.promise).mockResolvedValueOnce(session(2,blocked));
  let request!: Promise<void>;
  await act(()=>{request=view.hook.refreshReadiness();});
  await view.render(2);
  await act(async()=>{old.resolve(session(1));await request;});
  expect(view.hook.title).toBe("Campaign 2");
  expect(view.hook.readiness).toEqual(blocked);
});

it("re-reads shared state after another member's turn without a loading flash, and keeps the view if the read fails", async () => {
  vi.mocked(api.getSession).mockResolvedValue(session(1));
  const view = await mount();
  const firstBoot = view.hook.boot;
  vi.mocked(api.getSession).mockResolvedValueOnce({ ...session(1), current_scene_id: "cellar" } as SessionRead);
  vi.mocked(api.listSessionMessages).mockResolvedValueOnce([{ role: "dm", content: "Alice opened the door." }] as Awaited<ReturnType<typeof api.listSessionMessages>>);
  await act(() => view.hook.refreshShared());
  expect(view.hook.loading).toBe(false);
  expect(view.hook.sceneId).toBe("cellar");
  expect(view.hook.boot).not.toBe(firstBoot);
  expect(view.hook.boot?.history[0].content).toBe("Alice opened the door.");
  vi.mocked(api.getSession).mockRejectedValueOnce(new Error("offline"));
  await act(() => view.hook.refreshShared());
  expect(view.hook.sceneId).toBe("cellar");
  expect(view.hook.error).toBeNull();
});
