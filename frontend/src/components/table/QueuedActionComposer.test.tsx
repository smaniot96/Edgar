// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import type { SpotlightState } from "../../api/sessionChannel";
import type { QueuedAction } from "../../api/tablePacing";

const api = vi.hoisted(() => ({
  getQueue: vi.fn(),
  queueAction: vi.fn(),
  editQueuedAction: vi.fn(),
  cancelQueuedAction: vi.fn(),
}));
vi.mock("../../api/tablePacing", async (orig) => ({ ...(await orig<typeof import("../../api/tablePacing")>()), ...api }));

import { QueuedActionComposer } from "./QueuedActionComposer";

const member = (id: number, name: string) => ({
  character_id: id * 10, character_name: name, user_id: id, display_name: name, present: true,
});
const queued = (extra: Partial<QueuedAction> = {}): QueuedAction => ({
  id: "b".repeat(32), user_id: 2, display_name: "Bob", character_id: 20, character_name: "Bob",
  status: "queued", stale_reason: null, version: 1, created_at: "t", updated_at: "t", expires_at: "t",
  text: "I open the chest.", ...extra,
});
const state = (items: QueuedAction[] = [], holderId = 1): SpotlightState => ({
  session_id: 7, engaged: true, mode: "round_robin", version: 3, timeout_seconds: 300,
  holder: member(holderId, holderId === 1 ? "Alice" : "Bob"), since: null, handover_due: false,
  next_up: [member(holderId === 1 ? 2 : 1, holderId === 1 ? "Bob" : "Alice")],
  queue: { revision: 1, items },
});

let root: ReturnType<typeof createRoot> | null = null;
let container: HTMLDivElement | null = null;
afterEach(async () => {
  await act(() => root?.unmount());
  container?.remove();
  vi.clearAllMocks();
});

async function mount(ui: React.ReactElement) {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(() => root!.render(ui));
  return container;
}
const press = (el: Element | null | undefined) => act(async () => { (el as HTMLElement).click(); });
const button = (c: HTMLElement, label: string) =>
  [...c.querySelectorAll("button")].find((b) => b.textContent?.includes(label));

it("queues one action with the spotlight version the player saw, then refreshes the queue", async () => {
  api.queueAction.mockResolvedValue(queued());
  api.getQueue.mockResolvedValue({ revision: 2, items: [queued()] });
  const onQueue = vi.fn();
  const c = await mount(<QueuedActionComposer sessionId={7} viewerUserId={2} state={state()} now={0} onQueue={onQueue} />);
  expect(c.textContent).toContain("Waiting for Alice. You are next.");
  const area = c.querySelector("textarea")!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(area, "  I open the chest. ");
    area.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await press(button(c, "Queue action"));
  expect(api.queueAction).toHaveBeenCalledWith(7, { text: "I open the chest.", spotlightVersion: 3 });
  expect(onQueue).toHaveBeenCalledWith({ revision: 2, items: [queued()] });
});

it("shows a stale action with its reason, offers edit and cancel, and never offers play", async () => {
  const stale = queued({ status: "stale", stale_reason: "scene_changed" });
  api.cancelQueuedAction.mockResolvedValue(undefined);
  api.getQueue.mockResolvedValue({ revision: 3, items: [] });
  const c = await mount(<QueuedActionComposer sessionId={7} viewerUserId={2} state={state([stale])} now={0} />);
  expect(c.textContent).toContain("Out of date: the scene changed");
  expect(c.textContent).toContain("I open the chest.");
  expect(button(c, "Play my queued action")).toBeUndefined();
  expect(c.querySelector("textarea")).toBeNull(); // one queued action at a time
  await press(button(c, "Cancel queued action"));
  expect(api.cancelQueuedAction).toHaveBeenCalledWith(7, "b".repeat(32));
});

it("hands the queued action to the caller when it is the player's turn", async () => {
  const onPlay = vi.fn();
  const c = await mount(
    <QueuedActionComposer sessionId={7} viewerUserId={2} state={state([queued()], 2)} now={0} onPlay={onPlay} />,
  );
  expect(c.textContent).toContain("It is your turn.");
  await press(button(c, "Play my queued action"));
  expect(onPlay).toHaveBeenCalledWith(queued());
});

it("renders nothing on a solo or single-hero table", async () => {
  const solo = { ...state(), engaged: false };
  const c = await mount(<QueuedActionComposer sessionId={7} viewerUserId={2} state={solo} now={0} />);
  expect(c.innerHTML).toBe("");
});

it("shows a safe refusal and keeps the draft when the server refuses", async () => {
  const { ApiError } = await import("../../api/client");
  api.queueAction.mockRejectedValue(new ApiError(409, JSON.stringify({ code: "queued_action_exists" })));
  const c = await mount(<QueuedActionComposer sessionId={7} viewerUserId={2} state={state()} now={0} />);
  const area = c.querySelector("textarea")!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(area, "Go");
    area.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await press(button(c, "Queue action"));
  expect(c.querySelector("[role=alert]")?.textContent).toContain("already have a queued action");
  expect(c.querySelector("textarea")!.value).toBe("Go");
});
