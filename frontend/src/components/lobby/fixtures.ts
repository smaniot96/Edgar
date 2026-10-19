import type { ChannelEnvelope, SpotlightState } from "../../api/sessionChannel";
import type { LobbyViewer, SessionLobby } from "../../api/types";

export function spotlightFixture(overrides: Partial<SpotlightState> = {}): SpotlightState {
  return {
    session_id: 1,
    engaged: true,
    mode: "round_robin",
    version: 3,
    timeout_seconds: 300,
    holder: { character_id: 11, character_name: "Ayla", user_id: 1, display_name: "Alice", present: true },
    since: "2026-10-19T10:00:00Z",
    handover_due: false,
    next_up: [{ character_id: 12, character_name: "Bran", user_id: 2, display_name: "Bob", present: true }],
    ...overrides,
  };
}

export function lobbyFixture(viewer: Partial<LobbyViewer> = {}): SessionLobby {
  return {
    session_id: 1,
    campaign_id: 5,
    viewer: { user_id: 1, role: "owner", character_id: 11, character_name: "Ayla", ...viewer },
    members: [
      { user_id: 1, display_name: "Alice", role: "owner", character_id: 11, character_name: "Ayla" },
      { user_id: 2, display_name: "Bob", role: "player", character_id: 12, character_name: "Bran" },
      { user_id: 3, display_name: "Carol", role: "spectator", character_id: null, character_name: null },
    ],
  };
}

export function envelope(seq: number, type: string, data: Record<string, unknown> = {}, actor: ChannelEnvelope["actor"] = null): ChannelEnvelope {
  return { seq, type, channel: "system", actor, turn_id: null, at: "2026-10-19T10:00:00Z", data };
}
