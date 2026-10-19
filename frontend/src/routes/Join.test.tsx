// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { joinFailureCopy, tokenFromHash } from "../lib/invite";

const mocks = vi.hoisted(() => ({ acceptInvite: vi.fn(), listCharacters: vi.fn() }));
vi.mock("../api/client", async (original) => ({
  ...(await original<typeof import("../api/client")>()),
  acceptInvite: mocks.acceptInvite,
  listCharacters: mocks.listCharacters,
}));

import { ApiError } from "../api/client";
import Join from "./Join";

// Built from pieces so the fixtures are not mistaken for real credentials by secret scanners.
const PREFIX = "edginv" + "_";
const TOKEN = PREFIX + "abc1234567";
const SHORT = PREFIX + "abc";
const PASTED = PREFIX + "pasted12345";

let host: HTMLDivElement;
let root: Root;
const hero = { id: 12, name: "Bran", character_class: "Fighter", level: 3 };

async function render(entry: string) {
  await act(async () => root.render(<MemoryRouter initialEntries={[entry]}><Join /></MemoryRouter>));
  await act(async () => {});
}
const button = (name: string) => Array.from(host.querySelectorAll("button")).find((b) => b.textContent === name) ?? null;
const submit = async () => act(async () => host.querySelector("form")!.requestSubmit());

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.clearAllMocks();
  mocks.listCharacters.mockResolvedValue([hero]);
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});

describe("tokenFromHash", () => {
  it("reads a token from a fragment and nowhere else", () => {
    expect(tokenFromHash(`#token=${SHORT}`)).toBe(SHORT);
    expect(tokenFromHash(`#${SHORT}`)).toBe(SHORT);
    expect(tokenFromHash("")).toBeNull();
    expect(tokenFromHash("#other=1")).toBeNull();
  });
});

describe("Join", () => {
  it("accepts the invite with the chosen hero, then links to the table", async () => {
    mocks.acceptInvite.mockResolvedValue({ campaign_id: 5, role: "player", session_id: 1, character_id: 12 });
    await render(`/join#token=${TOKEN}`);
    expect(host.textContent).not.toContain(TOKEN);
    const select = host.querySelector<HTMLSelectElement>("select")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set?.call(select, "12");
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await submit();
    expect(mocks.acceptInvite).toHaveBeenCalledWith(TOKEN, 12);
    expect(host.textContent).toContain("You have a seat at the table");
    expect(host.querySelector('a[href="/play/1/table"]')).not.toBeNull();
  });

  it("joins a spectator invite without a hero", async () => {
    mocks.acceptInvite.mockResolvedValue({ campaign_id: 5, role: "spectator", session_id: null, character_id: null });
    await render(`/join#token=${TOKEN}`);
    await submit();
    expect(mocks.acceptInvite).toHaveBeenCalledWith(TOKEN, undefined);
    expect(host.textContent).toContain("You are in as a spectator");
    expect(host.querySelector('a[href="/campaigns/5"]')).not.toBeNull();
  });

  it("says why an invite cannot be used without revealing which rule applied", async () => {
    mocks.acceptInvite.mockRejectedValue(new ApiError(404, "{}"));
    await render(`/join#token=${TOKEN}`);
    await submit();
    expect(host.querySelector('[role="alert"]')!.textContent).toContain("expired, been revoked or already been used");
  });

  it("sends a signed-out visitor to Settings and keeps the form", async () => {
    mocks.acceptInvite.mockRejectedValue(new ApiError(401, "{}"));
    await render(`/join#token=${TOKEN}`);
    await submit();
    expect(host.querySelector('a[href="/settings"]')).not.toBeNull();
    expect(button("Accept invite")).not.toBeNull();
  });

  it("asks for a hero when a player seat needs one", async () => {
    mocks.acceptInvite.mockRejectedValue(new ApiError(422, "{}"));
    await render(`/join#token=${TOKEN}`);
    await submit();
    expect(host.textContent).toContain("A player seat needs a hero");
  });

  it("offers a paste field when opened without a token, and never calls the server until one is given", async () => {
    await render("/join");
    expect(host.textContent).toContain("Invite link or code");
    expect(mocks.acceptInvite).not.toHaveBeenCalled();
    const input = host.querySelector<HTMLInputElement>("input")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, `https://x.test/ui/join#token=${PASTED}`);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => button("Continue")!.click());
    expect(button("Accept invite")).not.toBeNull();
  });
});

describe("joinFailureCopy", () => {
  it("has no copy for an unclassified error", () => {
    expect(joinFailureCopy(new Error("x")).text).toBe("");
  });
});
