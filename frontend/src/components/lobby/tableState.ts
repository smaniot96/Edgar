/**
 * Pure table state for the lobby (EDG-050): how live channel events change the spotlight and the
 * presence list, who may act right now, and the roster rows the lobby renders. No React, no network,
 * so every rule here is unit-tested. The server stays authoritative for every action; these helpers
 * only decide what to show (and so avoid offering an input that the server would refuse).
 */
import type { ChannelActor, SpotlightMode, ChannelEnvelope, SpotlightChangedData, SpotlightState } from "../../api/sessionChannel";
import type { LobbyMember, LobbyViewer, MemberRole, PresenceEntry, SessionLobby } from "../../api/types";

export interface TurnActivity {
  actor: ChannelActor | null;
  stage: string | null;
}

export interface TableState {
  spotlight: SpotlightState | null;
  presence: Record<number, PresenceEntry>;
  /** A turn another member (or this one) is running right now. */
  activity: TurnActivity | null;
  /** Latest sentence for the screen-reader live region (turn changes, arrivals, departures). */
  announcement: string;
  /** Bumped when the REST roster may be out of date (a seat or an unknown member appeared). */
  rosterStale: number;
}

export const initialTableState: TableState = {
  spotlight: null,
  presence: {},
  activity: null,
  announcement: "",
  rosterStale: 0,
};

export type TableAction =
  | { type: "snapshot"; envelope: ChannelEnvelope }
  | { type: "event"; envelope: ChannelEnvelope }
  | { type: "spotlight"; spotlight: SpotlightState }
  | { type: "reset" };

function isSpotlight(value: unknown): value is SpotlightState {
  return typeof value === "object" && value !== null && typeof (value as SpotlightState).version === "number";
}

function presenceFromList(value: unknown): Record<number, PresenceEntry> {
  const out: Record<number, PresenceEntry> = {};
  if (!Array.isArray(value)) return out;
  for (const entry of value as PresenceEntry[]) {
    if (entry && typeof entry.user_id === "number") out[entry.user_id] = entry;
  }
  return out;
}

const REASON_COPY: Record<string, string> = {
  timeout: "The spotlight moved on because the previous player was idle.",
  turn_passed: "The turn passed on.",
  host_set: "The owner handed over the spotlight.",
  host_released: "The owner released the spotlight.",
  released: "The spotlight is open.",
};

/** What a screen-reader user hears when the spotlight changes. */
export function describeSpotlight(next: SpotlightState, reason?: string, selfUserId?: number | null): string {
  if (!next.engaged) return "The table is in free play.";
  const holder = next.holder;
  if (!holder) return "The spotlight is open. Any seated player may act.";
  const who = holder.user_id === selfUserId ? "It is your turn." : `It is now ${holder.character_name}'s turn${holder.display_name ? `, played by ${holder.display_name}` : ""}.`;
  const why = reason && REASON_COPY[reason] ? ` ${REASON_COPY[reason]}` : "";
  return `${who}${why}`;
}

function actorName(actor: ChannelActor | null | undefined): string {
  return actor?.display_name || actor?.character_name || "Someone";
}

export function tableReducer(state: TableState, action: TableAction, selfUserId: number | null = null): TableState {
  switch (action.type) {
    case "reset":
      return initialTableState;
    case "spotlight": {
      if (state.spotlight && action.spotlight.version < state.spotlight.version) return state;
      return { ...state, spotlight: action.spotlight };
    }
    case "snapshot": {
      // A snapshot is authoritative: replace, never merge.
      const data = action.envelope.data;
      return {
        ...state,
        spotlight: isSpotlight(data.spotlight_state) ? data.spotlight_state : state.spotlight,
        presence: presenceFromList(data.presence),
        activity: null,
      };
    }
    case "event":
      return applyEvent(state, action.envelope, selfUserId);
  }
}

function applyEvent(state: TableState, envelope: ChannelEnvelope, selfUserId: number | null): TableState {
  const { type, actor, data } = envelope;
  switch (type) {
    case "turn.spotlight_changed": {
      if (!isSpotlight(data)) return state;
      const next = data as unknown as SpotlightChangedData;
      // Events are delivered in order, but a REST read can be newer than an event still in flight.
      if (state.spotlight && next.version < state.spotlight.version) return state;
      const reason = typeof next.reason === "string" ? next.reason : "";
      const { reason: _reason, ...spotlight } = next;
      void _reason;
      return {
        ...state,
        spotlight: spotlight as SpotlightState,
        announcement: describeSpotlight(spotlight as SpotlightState, reason, selfUserId),
        rosterStale: reason.startsWith("seat_") ? state.rosterStale + 1 : state.rosterStale,
      };
    }
    case "turn.action":
      return { ...state, activity: { actor, stage: null } };
    case "turn.stage":
      return {
        ...state,
        activity: { actor: state.activity?.actor ?? actor, stage: typeof data.stage === "string" ? data.stage : null },
      };
    case "turn.finished":
      return { ...state, activity: null };
    case "presence.joined": {
      const userId = typeof data.user_id === "number" ? data.user_id : actor?.user_id;
      if (typeof userId !== "number") return state;
      const entry: PresenceEntry = {
        user_id: userId,
        display_name: actor?.display_name ?? null,
        character_id: actor?.character_id ?? null,
        character_name: actor?.character_name ?? null,
        state: "active",
      };
      return {
        ...state,
        presence: { ...state.presence, [userId]: entry },
        announcement: userId === selfUserId ? state.announcement : `${actorName(actor)} joined the table.`,
      };
    }
    case "presence.left": {
      const userId = typeof data.user_id === "number" ? data.user_id : actor?.user_id;
      if (typeof userId !== "number" || !state.presence[userId]) return state;
      const { [userId]: gone, ...rest } = state.presence;
      return { ...state, presence: rest, announcement: `${actorName(actor ?? gone)} left the table.` };
    }
    case "presence.idle":
    case "presence.active": {
      const userId = typeof data.user_id === "number" ? data.user_id : actor?.user_id;
      if (typeof userId !== "number" || !state.presence[userId]) return state;
      const wanted = type === "presence.idle" ? "idle" : "active";
      return { ...state, presence: { ...state.presence, [userId]: { ...state.presence[userId], state: wanted } } };
    }
    default:
      return state;
  }
}

export interface RosterRow {
  userId: number;
  name: string;
  role: MemberRole;
  characterId: number | null;
  characterName: string | null;
  present: boolean;
  idle: boolean;
  isHolder: boolean;
  /** 1-based place in the next-up order, or null. */
  nextUp: number | null;
  isYou: boolean;
}

const ROLE_ORDER: Record<MemberRole, number> = { owner: 0, player: 1, spectator: 2 };

export function memberName(member: { display_name: string | null }): string {
  return member.display_name?.trim() || "Unnamed player";
}

/** Everyone at the table with presence, seat and spotlight position merged. Players first, then spectators. */
export function buildRoster(
  lobby: SessionLobby | null,
  presence: Record<number, PresenceEntry>,
  spotlight: SpotlightState | null,
): RosterRow[] {
  if (!lobby) return [];
  const holderUser = spotlight?.engaged ? spotlight.holder?.user_id : undefined;
  const nextUp = new Map((spotlight?.next_up ?? []).map((m, i) => [m.user_id, i + 1] as const));
  const rows = lobby.members.map((m: LobbyMember): RosterRow => {
    const live = presence[m.user_id];
    return {
      userId: m.user_id,
      name: memberName(m),
      role: m.role,
      characterId: m.character_id,
      characterName: m.character_name,
      present: live !== undefined,
      idle: live?.state === "idle",
      isHolder: holderUser === m.user_id,
      nextUp: nextUp.get(m.user_id) ?? null,
      isYou: lobby.viewer.user_id === m.user_id,
    };
  });
  return rows.sort((a, b) => ROLE_ORDER[a.role] - ROLE_ORDER[b.role]);
}

export type TurnGateKind =
  /** No arbitration (solo, one hero) or role unknown: behave as before. */
  | "free"
  | "acting"
  /** Free-for-all with no holder: a seated player may claim the spotlight by acting. */
  | "open"
  | "waiting"
  | "spectating"
  | "unseated";

export interface TurnGate {
  kind: TurnGateKind;
  /** Whether the play screen should offer the action input. */
  canAct: boolean;
  /** Short heading for the banner. */
  title: string;
  detail: string;
  holderName: string | null;
}

/** Who may act now, mirroring the server's spotlight rule so the UI never offers a refused input. */
export function turnGate(viewer: LobbyViewer | null, spotlight: SpotlightState | null): TurnGate {
  if (!viewer) return { kind: "free", canAct: true, title: "", detail: "", holderName: null };
  if (viewer.role === "spectator") {
    return {
      kind: "spectating",
      canAct: false,
      title: "You are watching",
      detail: "Spectators follow the story but cannot act. Narration, rolls, the map and the journal update live.",
      holderName: spotlight?.holder?.character_name ?? null,
    };
  }
  // Local single-user mode has no accounts to arbitrate between.
  if (viewer.user_id === null) return { kind: "free", canAct: true, title: "", detail: "", holderName: null };
  const seated = viewer.character_id !== null;
  if (!spotlight || !spotlight.engaged) {
    if (viewer.role === "owner" || seated) return { kind: "free", canAct: true, title: "", detail: "", holderName: null };
    return {
      kind: "unseated",
      canAct: false,
      title: "You do not have a seat yet",
      detail: "Ask the owner for a table invite to seat one of your heroes. You can follow the story meanwhile.",
      holderName: null,
    };
  }
  const holder = spotlight.holder;
  if (holder && holder.user_id === viewer.user_id) {
    return {
      kind: "acting",
      canAct: true,
      title: "Your turn",
      detail: `The spotlight is on ${holder.character_name}. What do you do?`,
      holderName: holder.character_name,
    };
  }
  if (holder) {
    const who = holder.display_name ? `${holder.display_name} (${holder.character_name})` : holder.character_name;
    return {
      kind: "waiting",
      canAct: false,
      title: `Waiting for ${who}`,
      detail: "The story is shared: you will see their action and the narration as soon as it is saved.",
      holderName: holder.character_name,
    };
  }
  if (spotlight.mode === "free_for_all" && seated) {
    return {
      kind: "open",
      canAct: true,
      title: "The spotlight is open",
      detail: "Anyone seated may act. Your action takes the spotlight.",
      holderName: null,
    };
  }
  return {
    kind: "waiting",
    canAct: false,
    title: "Waiting for the spotlight",
    detail:
      viewer.role === "owner"
        ? "No one holds the spotlight. Hand it to a hero from the table panel."
        : "No one holds the spotlight yet. The owner or the next in order will take it.",
    holderName: null,
  };
}

const STAGE_COPY: Record<string, string> = {
  parsing: "reading the action",
  retrieving: "checking the rules",
  adjudicating: "ruling on it",
  narrating: "narrating",
  saving: "saving the turn",
};

export function stageLabel(stage: string | null | undefined): string | null {
  return stage ? STAGE_COPY[stage] ?? null : null;
}

export const MODE_LABEL: Record<SpotlightMode, string> = {
  round_robin: "Round robin",
  owner_directed: "Owner directed",
  free_for_all: "Free for all",
};

