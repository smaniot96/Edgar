// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { InviteRead } from "../../api/types";
import { lobbyFixture, spotlightFixture } from "./fixtures";
import { buildRoster, initialTableState, turnGate } from "./tableState";
import type { SessionLobbyState } from "./useSessionLobby";

const mocks = vi.hoisted(() => ({
  state: null as unknown as SessionLobbyState,
  refresh: vi.fn(),
  changeMemberRole: vi.fn(),
  removeMember: vi.fn(),
  leaveCampaign: vi.fn(),
  listInvites: vi.fn(),
  createInvite: vi.fn(),
  revokeInvite: vi.fn(),
  updateSpotlight: vi.fn(),
}));
vi.mock("./useSessionLobby", () => ({ useSessionLobby: () => mocks.state }));
vi.mock("../../api/client", async (original) => ({
  ...(await original<typeof import("../../api/client")>()),
  changeMemberRole: mocks.changeMemberRole,
  removeMember: mocks.removeMember,
  leaveCampaign: mocks.leaveCampaign,
  listInvites: mocks.listInvites,
  createInvite: mocks.createInvite,
  revokeInvite: mocks.revokeInvite,
  updateSpotlight: mocks.updateSpotlight,
}));

import { LobbyView } from "./LobbyView";

// Built from pieces so the fixture is not mistaken for a real credential by secret scanners.
const INVITE_TOKEN = "edginv" + "_secret" + "token123";

function build(over: Partial<SessionLobbyState> & { viewer?: Parameters<typeof lobbyFixture>[0] } = {}): SessionLobbyState {
  const { viewer, ...rest } = over;
  const lobby = lobbyFixture(viewer);
  const spotlight = spotlightFixture();
  const presence = { 1: { user_id: 1, display_name: "Alice", character_id: 11, character_name: "Ayla", state: "active" as const } };
  return {
    ...initialTableState,
    spotlight,
    presence,
    lobby,
    lobbyError: null,
    loaded: true,
    revoked: false,
    status: "live",
    roster: buildRoster(lobby, presence, spotlight),
    gate: turnGate(lobby.viewer, spotlight),
    refresh: mocks.refresh,
    ...rest,
  };
}

let host: HTMLDivElement;
let root: Root;
const text = () => host.textContent ?? "";
const button = (name: string | RegExp) =>
  Array.from(host.querySelectorAll("button")).find((b) => (typeof name === "string" ? b.textContent === name || b.getAttribute("aria-label") === name : name.test(b.textContent ?? "") || name.test(b.getAttribute("aria-label") ?? ""))) ?? null;

async function render() {
  await act(async () =>
    root.render(
      <MemoryRouter initialEntries={["/play/1/table"]}>
        <Routes>
          <Route path="/play/:sessionId/table" element={<LobbyView sessionId={1} />} />
          <Route path="/campaigns" element={<p>Campaigns page</p>} />
        </Routes>
      </MemoryRouter>,
    ),
  );
}
const change = async (el: HTMLSelectElement, value: string) =>
  act(async () => {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set?.call(el, value);
    el.dispatchEvent(new Event("change", { bubbles: true }));
  });

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.clearAllMocks();
  vi.spyOn(window, "confirm").mockReturnValue(true);
  mocks.listInvites.mockResolvedValue([]);
  mocks.changeMemberRole.mockResolvedValue({});
  mocks.removeMember.mockResolvedValue(undefined);
  mocks.leaveCampaign.mockResolvedValue(undefined);
  mocks.updateSpotlight.mockResolvedValue(spotlightFixture());
  mocks.state = build();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("LobbyView by role", () => {
  it("shows the owner presence, roles, seated heroes, whose turn it is and the next-up order", async () => {
    await render();
    expect(host.querySelector('[data-testid="spotlight-holder"]')?.textContent).toContain("Ayla (Alice)");
    expect(host.querySelector('[aria-label="Next up"]')?.textContent).toContain("Bran");
    const bob = host.querySelector('[data-testid="member-2"]')!.textContent!;
    expect(bob).toContain("Player");
    expect(bob).toContain("Playing Bran");
    expect(bob).toContain("Away");
    expect(host.querySelector('[data-testid="member-1"]')!.textContent).toContain("Here");
    expect(host.querySelector('[data-testid="member-3"]')!.textContent).toContain("Spectator");
    expect(text()).toContain("Invite people");
    expect(text()).toContain("Spotlight controls");
    expect(button("Leave campaign")).toBeNull();
  });

  it("gives a player the table and a leave button but no management controls", async () => {
    mocks.state = build({ viewer: { user_id: 2, role: "player", character_id: 12, character_name: "Bran" } });
    await render();
    expect(text()).toContain("You are playing Bran.");
    expect(text()).not.toContain("Invite people");
    expect(text()).not.toContain("Spotlight controls");
    expect(host.querySelector('select[id^="role-"]')).toBeNull();
    expect(button(/^Remove/)).toBeNull();
    expect(button("Leave campaign")).not.toBeNull();
  });

  it("gives a spectator a read-only table and a watch link", async () => {
    mocks.state = build({ viewer: { user_id: 3, role: "spectator", character_id: null, character_name: null } });
    await render();
    expect(text()).toContain("You are a spectator here.");
    expect(text()).toContain("Watch the adventure");
    expect(text()).not.toContain("Invite people");
    expect(button(/^Give to/)).toBeNull();
    expect(host.querySelector("textarea")).toBeNull();
  });
});

describe("LobbyView states", () => {
  it("shows a loading line, then an offline error with a retry", async () => {
    mocks.state = build({ loaded: false, lobby: null });
    await render();
    expect(text()).toContain("Setting the table");
    mocks.state = build({ loaded: true, lobby: null, lobbyError: new Error("net") });
    await render();
    expect(text()).toContain("Couldn't load the table");
    await act(async () => button("Check again")!.click());
    expect(mocks.refresh).toHaveBeenCalled();
  });

  it("tells a removed member that access ended, with a way out", async () => {
    mocks.state = build({ revoked: true, status: "closed" });
    await render();
    expect(text()).toContain("You no longer have access to this table");
    expect(host.querySelector('a[href="/campaigns"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="member-1"]')).toBeNull();
  });

  it("explains an offline connection and offers a reload when live updates stop", async () => {
    mocks.state = build({ status: "reconnecting" });
    await render();
    expect(text()).toContain("You are offline");
    expect(host.querySelector('[data-testid="connection-status"]')?.textContent).toBe("Reconnecting…");
    mocks.state = build({ status: "closed" });
    await render();
    expect(text()).toContain("Live updates stopped");
  });

  it("explains free play when there is no turn order", async () => {
    mocks.state = build({ spotlight: spotlightFixture({ engaged: false, holder: null, next_up: [] }) });
    await render();
    expect(text()).toContain("Free play");
  });
});

describe("owner party controls", () => {
  it("changes a member's role", async () => {
    await render();
    await change(host.querySelector<HTMLSelectElement>("#role-3")!, "player");
    expect(mocks.changeMemberRole).toHaveBeenCalledWith(5, 3, "player");
    expect(mocks.refresh).toHaveBeenCalled();
  });

  it("confirms before demoting a seated player, and does nothing if declined", async () => {
    vi.mocked(window.confirm).mockReturnValueOnce(false);
    await render();
    await change(host.querySelector<HTMLSelectElement>("#role-2")!, "spectator");
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("Their seat (Bran) is freed"));
    expect(mocks.changeMemberRole).not.toHaveBeenCalled();
  });

  it("removes a member after confirmation and refreshes", async () => {
    await render();
    await act(async () => button("Remove Bob from the campaign")!.click());
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("Remove Bob"));
    expect(mocks.removeMember).toHaveBeenCalledWith(5, 2);
    expect(mocks.refresh).toHaveBeenCalled();
  });

  it("shows a safe error when a member change is refused", async () => {
    const { ApiError } = await import("../../api/client");
    mocks.removeMember.mockRejectedValueOnce(new ApiError(409, JSON.stringify({ detail: "The campaign owner cannot be removed." })));
    await render();
    await act(async () => button("Remove Bob from the campaign")!.click());
    expect(host.querySelector('[role="alert"]')).not.toBeNull();
  });

  it("hands the spotlight over with the current version, and releases it", async () => {
    await render();
    await act(async () => button("Give to Bran")!.click());
    expect(mocks.updateSpotlight).toHaveBeenCalledWith(1, { holder_character_id: 12, expected_version: 3 });
    await act(async () => button("Release")!.click());
    expect(mocks.updateSpotlight).toHaveBeenLastCalledWith(1, { holder_character_id: null, expected_version: 3 });
    expect(button("Ayla has it")!.disabled).toBe(true);
  });

  it("sets the mode and the idle timeout", async () => {
    await render();
    await change(host.querySelector<HTMLSelectElement>("select[id]:not([id^='role-'])")!, "free_for_all");
    expect(mocks.updateSpotlight).toHaveBeenCalledWith(1, { mode: "free_for_all", expected_version: 3 });
    const input = host.querySelector<HTMLInputElement>('input[type="number"]')!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, "10");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(text()).toContain("Enter a whole number from 30 to 3600.");
    expect(button("Save")!.disabled).toBe(true);
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, "120");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => button("Save")!.click());
    expect(mocks.updateSpotlight).toHaveBeenLastCalledWith(1, { timeout_seconds: 120, expected_version: 3 });
  });
});

describe("invites", () => {
  const created = { id: 9, campaign_id: 5, session_id: 1, role: "player", invited_email: null, status: "active", created_at: "2026-10-19T10:00:00Z", expires_at: "2026-10-22T10:00:00Z", accepted_at: null, revoked_at: null, token: INVITE_TOKEN };

  it("creates a session-bound player invite, shows the link once with a copy button, and forgets it", async () => {
    mocks.createInvite.mockResolvedValue(created);
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    await render();
    await act(async () => button("Create invite link")!.click());
    expect(mocks.createInvite).toHaveBeenCalledWith(5, { role: "player", expires_in_hours: 72, session_id: 1 });
    const field = host.querySelector<HTMLInputElement>("#invite-link")!;
    expect(field.value).toContain(`/join#token=${INVITE_TOKEN}`);
    expect(text()).toContain("shown only once");
    await act(async () => button("Copy link")!.click());
    expect(writeText).toHaveBeenCalledWith(field.value);
    expect(text()).toContain("Link copied.");
    await act(async () => button("Done")!.click());
    expect(host.querySelector("#invite-link")).toBeNull();
    expect(host.innerHTML).not.toContain("secrettoken123");
  });

  it("falls back to a selectable field when the clipboard is unavailable", async () => {
    mocks.createInvite.mockResolvedValue({ ...created, role: "spectator", session_id: null });
    vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) } });
    await render();
    await change(host.querySelector<HTMLSelectElement>("select")!.closest("div")!.querySelector("select")!, "player");
    await act(async () => button("Create invite link")!.click());
    await act(async () => button("Copy link")!.click());
    expect(text()).toContain("Could not copy automatically");
    expect(host.querySelector("#invite-link")).not.toBeNull();
  });

  it("lists invites and revokes an active one", async () => {
    const active: InviteRead = { ...created, status: "active" } as InviteRead;
    const used: InviteRead = { ...created, id: 8, status: "accepted" } as InviteRead;
    mocks.listInvites.mockResolvedValue([used, active]);
    await render();
    await act(async () => {});
    expect(text()).toContain("accepted");
    await act(async () => button("Revoke invite 9")!.click());
    expect(mocks.revokeInvite).toHaveBeenCalledWith(5, 9);
    expect(button("Revoke invite 8")).toBeNull();
  });
});

describe("leaving", () => {
  it("lets a player leave after confirming and returns to campaigns", async () => {
    mocks.state = build({ viewer: { user_id: 2, role: "player", character_id: 12, character_name: "Bran" } });
    await render();
    await act(async () => button("Leave campaign")!.click());
    expect(mocks.leaveCampaign).toHaveBeenCalledWith(5);
    expect(text()).toContain("Campaigns page");
  });
});
