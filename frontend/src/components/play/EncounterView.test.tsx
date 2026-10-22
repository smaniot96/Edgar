// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { EncounterView } from "./EncounterView";
import type { CombatState } from "../../api/types";

it("uses stable actor selection, shows engine positions and events, and only drafts a target on explicit request", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const onSelectTarget = vi.fn();
  const combat: CombatState = { round: 4, ended: false, outcome: null, combatants: [
    { id: "hero:1", name: "Mira", is_player: true, hp_current: 11, hp_max: 44, alive: true },
    { id: "foe:a", name: "Guard", is_player: false, hp_current: 6, hp_max: 11, alive: true, band: "melee" },
    { id: "foe:b", name: "Guard", is_player: false, hp_current: 9, hp_max: 11, alive: true, band: "ranged" },
    { id: "foe:c", name: "Legacy foe", is_player: false, hp_current: 9, hp_max: 11, alive: true },
  ], participants: [
    { id: "actor:sheep", name: "The sheep", participation: "visible_nonparticipant", reason: "Visible in the scene; no turn in this encounter’s initiative." },
    { id: "foe:a", name: "Guard", participation: "active", reason: "Active combatant." },
  ], round_events: [{ kind: "withdraw", text: "Mira withdrew without provoking an opportunity attack." }, { kind: "close", text: "Guard closed on its own turn." }] };
  const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
  try {
    await act(() => root.render(<EncounterView combat={combat} onSelectTarget={onSelectTarget} />));
    expect(host.textContent).toContain("Schematic, not to scale");
    expect(host.querySelector('[aria-label="Position unknown"]')?.textContent).toContain("Legacy foe");
    const bystanders = host.querySelector('[aria-label="Visible nonparticipants"]')!;
    expect(bystanders.textContent).toContain("The sheep");
    expect(bystanders.textContent).toContain("no turn in this encounter’s initiative");
    expect(bystanders.textContent).not.toContain("Guard");
    expect(bystanders.querySelector("button")).toBeNull();
    const ranged = host.querySelector<HTMLButtonElement>('[aria-label="At range"] button')!;
    await act(() => ranged.click());
    expect(ranged.getAttribute("aria-pressed")).toBe("true");
    expect(host.querySelector('[aria-label="In melee"] button')?.getAttribute("aria-pressed")).toBe("false");
    expect(onSelectTarget).not.toHaveBeenCalled();
    await act(() => Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "Add target to draft")!.click());
    expect(onSelectTarget).toHaveBeenCalledExactlyOnceWith("Guard");
    expect(Array.from(host.querySelectorAll('[aria-label="Last resolved round"] li')).map((item) => item.textContent)).toEqual(combat.round_events!.map((event) => event.text));
    await act(() => root.render(<EncounterView combat={{ ...combat, ended: true }} onSelectTarget={onSelectTarget} />));
    expect(Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "Add target to draft")?.disabled).toBe(true);
  } finally { await act(() => root.unmount()); host.remove(); vi.unstubAllGlobals(); }
});
