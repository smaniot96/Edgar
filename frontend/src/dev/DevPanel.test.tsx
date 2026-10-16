// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import type { DebugPayload, TurnDiagnostics } from "../api/types";
import { DevPanel } from "./DevPanel";

vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);

const roots: ReturnType<typeof createRoot>[] = [];
afterEach(() => {
  roots.splice(0).forEach((root) => act(() => root.unmount()));
  document.body.innerHTML = "";
});

function debugWith(diag: TurnDiagnostics): DebugPayload {
  return {
    parsed_input: null,
    rules_context: [],
    adventure_context: [],
    rules_context_count: 0,
    adventure_context_count: 0,
    world_flags_in: {},
    combat_state: null,
    timings_ms: {},
    model: "gpt-test",
    turn_diagnostics: diag,
  } as DebugPayload;
}

const partialTurn: TurnDiagnostics = {
  scope: "dm_turn",
  version: 1,
  turn_id: "t-1",
  path: "normal",
  delivery: "stream",
  total_ms: 812,
  time_to_first_text_ms: null,
  stages_ms: { narrating_ms: 640 },
  stages_unavailable: ["lock_wait_ms", "parsing_ms"],
  lock: null,
  retrieval: null,
  provider_calls: [
    {
      kind: "narrate",
      profile: "play.narrator",
      requested_model: "gpt-test",
      resolved_model: null,
      streamed: true,
      latency_ms: 640,
      first_text_ms: null,
      attempts: null,
      error_category: null,
      usage: { input_tokens: null, output_tokens: null, total_tokens: null },
      cost_microusd: null,
    },
  ],
  totals: {
    calls: 1,
    calls_dropped: 0,
    input_tokens: null,
    output_tokens: null,
    total_tokens: null,
    cost_microusd: null,
    calls_unknown_usage: 1,
    calls_unknown_cost: 1,
    complete: false,
  },
};

async function openLatencySection(container: HTMLElement) {
  const toggle = [...container.querySelectorAll("button")].find((b) =>
    b.textContent?.includes("Latency, usage & cost"),
  );
  expect(toggle).toBeTruthy();
  await act(async () => toggle!.click());
}

async function render(debug: DebugPayload | null) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  roots.push(root);
  await act(async () => root.render(<DevPanel debug={debug} />));
  return container;
}

it("shows unknown usage, cost and first-text values, and labels the total as partial", async () => {
  const container = await render(debugWith(partialTurn));
  await openLatencySection(container);

  const text = container.textContent ?? "";
  expect(text).toContain("time to first text");
  expect(text).toContain("unknown");
  expect(text).toContain("partial total: 1 call(s) without usage, 1 without cost");
  expect(text).toContain("in unknown / out unknown");
  // Nothing unmeasured is rendered as a zero token count or a zero cost.
  expect(text).not.toMatch(/tokens in\s*0\b/);
  expect(text).not.toContain("$0.000000");
  expect(text).not.toMatch(/time to first text\s*0 ms/);
});

it("renders a measured zero as zero and a complete total without the partial label", async () => {
  const complete: TurnDiagnostics = {
    ...partialTurn,
    lock: { wait_ms: 0, contended: false, attempts: null },
    time_to_first_text_ms: 0,
    provider_calls: [
      {
        ...partialTurn.provider_calls[0],
        resolved_model: "gpt-test",
        first_text_ms: 0,
        usage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
        cost_microusd: 0,
      },
    ],
    totals: {
      ...partialTurn.totals,
      input_tokens: 0,
      output_tokens: 0,
      total_tokens: 0,
      cost_microusd: 0,
      calls_unknown_usage: 0,
      calls_unknown_cost: 0,
      complete: true,
    },
  };
  const container = await render(debugWith(complete));
  await openLatencySection(container);

  const text = container.textContent ?? "";
  expect(text).toContain("tokens in0");
  expect(text).toContain("$0.000000");
  expect(text).toContain("lock wait0 ms");
  expect(text).not.toContain("partial total");
});

it("says the turn's latency was not captured when an older build sent no row", async () => {
  const container = await render({ ...debugWith(partialTurn), turn_diagnostics: undefined } as DebugPayload);
  await openLatencySection(container);

  expect(container.textContent).toContain("unknown: not captured for this turn");
});
