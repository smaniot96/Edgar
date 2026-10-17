// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import type { TurnTraceRecord } from "../api/types";
import type { TraceState } from "../components/play/turnState";
import { DevPanel } from "./DevPanel";

vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);

const roots: ReturnType<typeof createRoot>[] = [];
afterEach(() => {
  roots.splice(0).forEach((root) => act(() => root.unmount()));
  document.body.innerHTML = "";
});

/** Renders the panel and opens every collapsed section, so the assertions see the stored content. */
function render(props: Parameters<typeof DevPanel>[0]): string {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  roots.push(root);
  act(() => root.render(<DevPanel {...props} />));
  act(() => {
    host.querySelectorAll("button").forEach((button) => {
      if (button.textContent?.includes("▸")) button.click();
    });
  });
  return host.textContent ?? "";
}

const PLANNER_TEXT = "The hidden witness is the baron's cousin";
const SELECTOR = "adventure-reviewed-policy-7f3a";

const captured: TurnTraceRecord = {
  turn_id: "turn-a",
  created_at: "2026-10-17T10:00:01Z",
  combat_round: null,
  turn_diagnostics: null,
  trace: {
    version: 1,
    scope: "dm_turn",
    turn_id: "turn-a",
    delivery: "stream",
    path: "normal",
    parsed_input: {
      intent: "exploration",
      check: "perception",
      difficulty: "easy",
      target: "door",
      retrieval_query: "notice a hidden door",
      uses_item: null,
      lethality: null,
      confirms_ending: false,
      entities: { npc: "Mara" },
    },
    rag: {
      rules: {
        count: 1,
        chunks_dropped: 0,
        chunks: [
          {
            collection: "rules_phb",
            source: "PHB",
            page: 49,
            score: 0.75,
            text: "Grappling uses Athletics.",
            text_withheld: false,
            citation: { rules_edition: "2014", edition_status: "compatible", source_sha256: "a".repeat(64) },
          },
        ],
      },
      adventure: {
        count: 1,
        chunks_dropped: 0,
        chunks: [
          {
            collection: "adventure_tower",
            source: "ruined_tower",
            page: 3,
            score: 0.5,
            text_withheld: true,
            citation: { rules_edition: "2014", audience: "planner" },
          },
        ],
      },
    },
    settings: {
      rules_edition: "2014",
      source_mode: "module",
      narrator_profile: "play.narrator",
      profile_version: 3,
      profiles: { "play.narrator": { model: "gpt-test-narrator", reasoning_effort: "low", api: "responses" } },
    },
    composed_with: ["turn_diagnostics", "combat_round"],
  },
};

it("a captured trace reloads its intent, evidence and settings instead of claiming no debug", () => {
  const text = render({ adjudication: null, trace: { status: "captured", record: captured } });
  expect(text).not.toContain("No debug captured");
  expect(text).toContain("notice a hidden door");
  expect(text).toContain("Grappling uses Athletics.");
  expect(text).toContain("gpt-test-narrator");
  expect(text).toContain("Text not stored");
  expect(text).not.toContain(PLANNER_TEXT);
  expect(text).not.toContain(SELECTOR);
});

it("a captured turn without companion rows says so instead of inventing numbers", () => {
  const text = render({ trace: { status: "captured", record: captured } });
  expect(text).toContain("no measurement stored for this turn");
  expect(text).toContain("Not a combat turn.");
});

const unavailable: [string, TraceState | null | undefined, string][] = [
  ["not captured (Player mode or legacy)", { status: "not_captured" }, "No developer trace was stored"],
  ["developer debug disabled", { status: "disabled" }, "Developer debug is off on this server"],
  ["loading", { status: "loading" }, "Loading the stored developer trace"],
  ["older than the window", { status: "older_than_window" }, "older than the loaded trace window"],
  ["failed read", { status: "failed" }, "could not be loaded"],
  ["live turn with no debug frame", undefined, "No debug captured for this turn (sent in User mode)."],
];

it.each(unavailable)("shows an honest unavailable reason: %s", (_name, trace, reason) => {
  const text = render({ trace });
  expect(text).toContain(reason);
  expect(text).not.toContain("gpt-test-narrator");
});

it("with an adjudication but no trace, the reason is shown beside the ruling", () => {
  const text = render({
    trace: { status: "not_captured" },
    adjudication: { success: true, mechanical_summary: "Hit.", dice_result: 17 } as never,
  });
  expect(text).toContain("No developer trace was stored");
  expect(text).toContain("Adjudication");
});
