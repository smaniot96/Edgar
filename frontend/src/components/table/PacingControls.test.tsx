// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

import type { PacingState } from "../../api/tablePacing";

const api = vi.hoisted(() => ({ setPacingPolicy: vi.fn() }));
vi.mock("../../api/tablePacing", async (orig) => ({ ...(await orig<typeof import("../../api/tablePacing")>()), ...api }));

import { PacingControls } from "./PacingControls";

const pacing: PacingState = {
  policy: "standard", label: "Standard", summary: "Five minutes per turn.", timeout_seconds: 300,
  auto_pass: false, queue_ttl_seconds: 3600,
};

async function mount(ui: React.ReactElement) {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const c = document.createElement("div");
  document.body.append(c);
  const root = createRoot(c);
  await act(() => root.render(ui));
  return { c, done: () => act(() => root.unmount()) };
}

it("shows everyone the policy but lets only the host change it", async () => {
  const player = await mount(<PacingControls sessionId={7} pacing={pacing} canChange={false} />);
  expect(player.c.textContent).toContain("Standard");
  expect(player.c.textContent).toContain("Five minutes per turn.");
  expect(player.c.querySelector("[role=radiogroup]")).toBeNull();
  await player.done();

  const onChanged = vi.fn();
  api.setPacingPolicy.mockResolvedValue({ version: 9 });
  const host = await mount(<PacingControls sessionId={7} pacing={pacing} canChange onChanged={onChanged} />);
  const radios = [...host.c.querySelectorAll<HTMLElement>("[role=radio]")];
  expect(radios.map((r) => r.getAttribute("aria-checked"))).toEqual(["false", "true", "false"]);
  await act(async () => radios[2].click());
  expect(api.setPacingPolicy).toHaveBeenCalledWith(7, "fast");
  expect(onChanged).toHaveBeenCalledWith({ version: 9 });
  await host.done();
});

it("renders nothing before the table state arrives", async () => {
  const empty = await mount(<PacingControls sessionId={7} pacing={undefined} canChange />);
  expect(empty.c.innerHTML).toBe("");
  await empty.done();
});
