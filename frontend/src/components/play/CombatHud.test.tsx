// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import type { CombatState } from "../../api/types";
import { CombatHud } from "./CombatHud";

const combat = {
  round: 1,
  ended: false,
  combatants: [],
  initiative: [],
  current_turn_id: null,
  current_turn: null,
  current_turn_index: null,
  player_ac: null,
  second_wind_used: false,
  economy: null,
  raging: false,
  concentration: null,
  pending_declaration: { summary: "Confirm before any dice: Eda will close in, then attack Goblin." },
} as unknown as CombatState;

function render(node: React.ReactElement) {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => root.render(node));
  return { host, unmount: () => act(() => root.unmount()) };
}

it("shows a waiting declaration and sends yes or no as the reply", async () => {
  const onReply = vi.fn();
  const { host, unmount } = render(<CombatHud combat={combat} onReply={onReply} />);
  const group = host.querySelector('[aria-label="Confirm declaration"]');
  expect(group?.textContent).toContain("Confirm before any dice");
  const [confirm, cancel] = Array.from(group?.querySelectorAll("button") ?? []);
  await act(async () => confirm.click());
  await act(async () => cancel.click());
  expect(onReply.mock.calls).toEqual([["yes"], ["no"]]);
  unmount();
});

it("shows no confirmation when none is waiting or the encounter has ended", () => {
  const { host, unmount } = render(<CombatHud combat={{ ...combat, pending_declaration: null }} />);
  expect(host.querySelector('[aria-label="Confirm declaration"]')).toBeNull();
  unmount();
  const ended = render(<CombatHud combat={{ ...combat, ended: true }} />);
  expect(ended.host.querySelector('[aria-label="Confirm declaration"]')).toBeNull();
  ended.unmount();
});
