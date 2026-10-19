/**
 * Table pacing and queued intended actions (EDG-051, docs/session-broadcast.md "Table pacing").
 *
 * Hook-free: types, the pure rules the lobby needs (who is waiting on whom, whether a player may
 * queue, how a queued action becomes a turn) and thin API calls. Ordering and validation are the
 * server's: nothing here decides whose turn it is or whether a queued action is still valid.
 * The lobby (EDG-050) keeps one `SpotlightState`, feeds every channel record through
 * `applyPacingEvent`, and mounts `components/table/PacingControls` and `QueuedActionComposer`.
 */

import { apiFetch } from "./client";
import type { ChannelEnvelope, SpotlightState } from "./sessionChannel";

export type PacingPolicyName = "relaxed" | "standard" | "fast";
export type PacingPolicy = PacingPolicyName | "custom";

export interface PacingState {
  policy: PacingPolicy;
  label: string;
  summary: string;
  timeout_seconds: number;
  /** A holder who has left the table is passed early (fast policy). */
  auto_pass: boolean;
  queue_ttl_seconds: number;
}

export type QueuedStatus = "queued" | "stale" | "expired";
export type StaleReason = "scene_changed" | "combat_changed" | "hero_state_changed";

export interface QueuedAction {
  id: string;
  user_id: number;
  display_name: string | null;
  character_id: number;
  character_name: string | null;
  status: QueuedStatus;
  stale_reason: StaleReason | null;
  version: number;
  created_at: string;
  updated_at: string;
  expires_at: string;
  /** Present only for the author. The table sees that an action is queued, never what it says. */
  text?: string | null;
}

export interface QueueState {
  /** Increases on every change; an older state never replaces a newer one. */
  revision: number;
  items: QueuedAction[];
}

export const PACING_POLICIES: ReadonlyArray<{ value: PacingPolicyName; label: string; blurb: string }> = [
  { value: "relaxed", label: "Relaxed", blurb: "Play at your own pace: an hour per turn, queued actions last a day." },
  { value: "standard", label: "Standard", blurb: "Five minutes per turn, queued actions last an hour." },
  { value: "fast", label: "Fast", blurb: "A holder who left is passed after 20 seconds, an idle one after a minute." },
];

// --- Channel state -------------------------------------------------------------------------------

/** Replace the queue only with a newer or equal revision (events can arrive out of order across workers). */
export function nextQueue(current: QueueState | undefined, incoming: QueueState): QueueState {
  if (current && incoming.revision < current.revision) return current;
  return incoming;
}

/**
 * Fold one channel record into the shared spotlight state. `turn.spotlight_changed` replaces the
 * state (guarding its version), `turn.queue_changed` replaces only the queue (guarding its revision).
 * Returns the same object when the record changes nothing.
 */
export function applyPacingEvent(state: SpotlightState | null, envelope: ChannelEnvelope): SpotlightState | null {
  if (envelope.type === "turn.spotlight_changed") {
    const next = envelope.data as unknown as SpotlightState;
    if (state && next.version < state.version) return state;
    const queue = state?.queue && next.queue ? nextQueue(state.queue, next.queue) : next.queue;
    return { ...next, queue };
  }
  if (envelope.type === "turn.queue_changed" && state) {
    const incoming = envelope.data as unknown as QueueState;
    if (!Array.isArray(incoming.items) || typeof incoming.revision !== "number") return state;
    const queue = nextQueue(state.queue, incoming);
    if (queue === state.queue) return state;
    const queued = new Set(queue.items.filter((i) => i.status !== "expired").map((i) => i.user_id));
    return { ...state, queue, next_up: state.next_up.map((m) => ({ ...m, queued: queued.has(m.user_id) })) };
  }
  return state;
}

// --- Waiting state -------------------------------------------------------------------------------

export type WaitingKind = "not_engaged" | "your_turn" | "waiting" | "free" | "host_directed";

export interface WaitingState {
  kind: WaitingKind;
  holderName: string | null;
  /** Turns before yours: 0 when it is your turn or the table is not engaged. */
  turnsAhead: number;
  /** Seconds until an idle holder loses the spotlight, or null. Never negative. */
  secondsLeft: number | null;
  /** The holder is no longer on the channel (they may be passed early on the fast policy). */
  holderAbsent: boolean;
  message: string;
}

/** Who is the table waiting on, and where is this viewer in the line. `now` is a ms timestamp. */
export function waitingState(state: SpotlightState, viewerUserId: number, now: number): WaitingState {
  const base = { holderName: null, turnsAhead: 0, secondsLeft: null, holderAbsent: false };
  if (!state.engaged) return { ...base, kind: "not_engaged", message: "" };
  const holder = state.holder;
  if (!holder) {
    const hostDirected = state.mode === "owner_directed";
    return {
      ...base,
      kind: hostDirected ? "host_directed" : "free",
      message: hostDirected
        ? "The host will hand over the spotlight."
        : "The spotlight is free. Take it when you are ready.",
    };
  }
  const name = holder.display_name ?? holder.character_name;
  const deadline = state.turn_deadline ? Date.parse(state.turn_deadline) : NaN;
  const secondsLeft = Number.isFinite(deadline) ? Math.max(0, Math.ceil((deadline - now) / 1000)) : null;
  if (holder.user_id === viewerUserId) {
    return { ...base, kind: "your_turn", holderName: name, secondsLeft, message: "It is your turn." };
  }
  const index = state.next_up.findIndex((m) => m.user_id === viewerUserId);
  const turnsAhead = state.mode === "round_robin" && index >= 0 ? index + 1 : 0;
  const place = turnsAhead > 0 ? ` You are ${turnsAhead === 1 ? "next" : `${turnsAhead} turns away`}.` : "";
  return {
    kind: "waiting",
    holderName: name,
    turnsAhead,
    secondsLeft,
    holderAbsent: !holder.present,
    message: `Waiting for ${name}.${place}`,
  };
}

/** Whether the viewer may queue: engaged table, someone else holds the spotlight, no action queued yet. */
export function canQueue(state: SpotlightState, viewerUserId: number): boolean {
  const wait = waitingState(state, viewerUserId, 0);
  return wait.kind === "waiting" && myQueuedAction(state, viewerUserId) === null;
}

export function myQueuedAction(state: SpotlightState, viewerUserId: number): QueuedAction | null {
  return state.queue?.items.find((i) => i.user_id === viewerUserId) ?? null;
}

/** Plain-language status of a queued action, for the author and the table. */
export function queuedStatusText(item: QueuedAction): string {
  if (item.status === "expired") return "Expired";
  if (item.status === "stale") {
    const why: Record<StaleReason, string> = {
      scene_changed: "the scene changed",
      combat_changed: "combat started or ended",
      hero_state_changed: "your hero's condition changed",
    };
    return `Out of date: ${item.stale_reason ? why[item.stale_reason] : "the situation changed"}`;
  }
  return "Queued";
}

/**
 * The turn request fields that play a queued action. The message must be the stored text and the
 * version the one the player last saw; the server validates it again and refuses a stale action.
 */
export function queuedTurnFields(item: QueuedAction): {
  message: string;
  queued_action_id: string;
  queued_action_version: number;
} {
  return { message: item.text ?? "", queued_action_id: item.id, queued_action_version: item.version };
}

// --- API -------------------------------------------------------------------------------------------

async function readJson<T>(res: Response): Promise<T> {
  return (await res.json()) as T;
}

function jsonInit(method: string, body: unknown): RequestInit {
  return { method, headers: { "content-type": "application/json" }, body: JSON.stringify(body) };
}

export function getQueue(sessionId: number): Promise<QueueState> {
  return apiFetch(`/api/sessions/${sessionId}/queue`).then((r) => readJson<QueueState>(r));
}

/** A retried submission with the same `requestId` returns the same item, never a second one. */
export function queueAction(
  sessionId: number,
  body: { text: string; requestId?: string; spotlightVersion?: number },
): Promise<QueuedAction> {
  return apiFetch(
    `/api/sessions/${sessionId}/queue`,
    jsonInit("POST", { text: body.text, request_id: body.requestId, spotlight_version: body.spotlightVersion }),
  ).then((r) => readJson<QueuedAction>(r));
}

export function editQueuedAction(
  sessionId: number,
  action: Pick<QueuedAction, "id" | "version">,
  text: string,
): Promise<QueuedAction> {
  return apiFetch(
    `/api/sessions/${sessionId}/queue/${action.id}`,
    jsonInit("PATCH", { text, expected_version: action.version }),
  ).then((r) => readJson<QueuedAction>(r));
}

export async function cancelQueuedAction(sessionId: number, actionId: string): Promise<void> {
  await apiFetch(`/api/sessions/${sessionId}/queue/${actionId}`, { method: "DELETE" });
}

/** Owner only. Sets the named table pacing policy (and with it the turn timeout). */
export function setPacingPolicy(sessionId: number, pacing: PacingPolicyName): Promise<SpotlightState> {
  return apiFetch(`/api/sessions/${sessionId}/spotlight`, jsonInit("PATCH", { pacing })).then((r) =>
    readJson<SpotlightState>(r),
  );
}
