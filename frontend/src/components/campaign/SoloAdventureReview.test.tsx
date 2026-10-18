// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  ApiError,
  getCampaign,
  getSoloAdventureReview,
  inspectAdventureManifest,
  publishAdventureManifest,
  recordSoloSpoilerReview,
} from "../../api/client";
import type { CampaignRead, SoloAdventureReview as SoloReview } from "../../api/types";
import { AdventureReview } from "./AdventureReview";

vi.mock("../../api/client", async (original) => ({
  ...await original<object>(),
  inspectAdventureManifest: vi.fn(),
  compileAdventureManifest: vi.fn(),
  saveAdventureManifest: vi.fn(),
  publishAdventureManifest: vi.fn(),
  getCampaign: vi.fn(),
  getSoloAdventureReview: vi.fn(),
  recordSoloSpoilerReview: vi.fn(),
}));

const SECRET = "The keeper hides the vault key under the dock.";
const DIGEST = "b".repeat(64);

const campaign: CampaignRead = {
  id: 7, title: "Salt Gate", system: "5e", created_by: 1, created_at: "2026-10-18",
  adventure_collections: ["salt_gate"], source_mode: "module", status: "active", ended_at: null,
  retrieval_degraded: false, play_allowed: false,
  readiness: {
    play_allowed: false, history_allowed: true, state: "adventure_review_needed", primary_action: "review_adventure",
    blockers: [{ code: "adventure_review_needed", message: "Review and publish the adventure before starting or continuing play.", action: "review_adventure" }],
  },
  sources: [{
    slug: "salt_gate", title: "Salt Gate", status: "ready", usable_chunks: 4, expected_chunks: 4,
    collection_exists: true, source_pdf_available: false, message: "The source is ready for module play.",
  }],
};

const counts = { passages: 3, player: 2, planner: 1, excluded: 0, unreviewed: 0, scenes: 2, entities: 3, resolution_policies: 0, coverage_percent: 100 };

function solo(overrides: Partial<SoloReview>): SoloReview {
  return {
    mode: "solo", state: "ready", title: "Salt Gate", rules_edition: "2014", checks: [], warnings: [],
    counts, reviewed_digest: DIGEST, reviewer: "Solo host (spoiler-safe review)", ...overrides,
  };
}

const passedChecks = [
  { code: "spans_match_source", status: "passed" as const, message: "Every source span matches the retained extraction.", count: null, recovery: null },
  { code: "passages_classified", status: "passed" as const, message: "Every passage has an explicit audience.", count: null, recovery: null },
];

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.clearAllMocks();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

function button(label: string): HTMLButtonElement {
  const found = [...container.querySelectorAll("button")].find((b) => b.textContent === label);
  if (!found) throw new Error(`missing button ${label}`);
  return found;
}

async function renderWith(review: SoloReview) {
  vi.mocked(getSoloAdventureReview).mockResolvedValue(review);
  await act(() => root.render(<AdventureReview campaign={campaign} onUpdated={vi.fn()} />));
}

it("publishes a passed solo review through the existing publish route without loading passage text", async () => {
  const onUpdated = vi.fn();
  vi.mocked(getSoloAdventureReview).mockResolvedValue(solo({ checks: passedChecks }));
  vi.mocked(publishAdventureManifest).mockResolvedValue({ status: "ready", chunks: 3 });
  vi.mocked(getCampaign).mockResolvedValue({ ...campaign, readiness: { ...campaign.readiness!, play_allowed: true, blockers: [] } });
  await act(() => root.render(<AdventureReview campaign={campaign} onUpdated={onUpdated} />));

  expect(container.textContent).toContain("Every automatic check passed");
  expect(container.textContent).toContain("Player text");
  expect(container.textContent).not.toContain(SECRET);
  expect(inspectAdventureManifest).not.toHaveBeenCalled();

  await act(async () => { button("Publish and play").click(); });
  expect(publishAdventureManifest).toHaveBeenCalledWith("salt_gate", DIGEST, "Solo host (spoiler-safe review)");
  expect(onUpdated).toHaveBeenCalled();
  expect(container.textContent).toContain("Published. Play and the session map use this reviewed version.");
});

it("explains a failed check by name and count and offers regeneration, never passage text", async () => {
  await renderWith(solo({
    state: "blocked",
    reviewed_digest: null,
    checks: [{
      code: "passages_classified", status: "failed", message: "3 passage(s) still have no explicit audience.",
      count: 3, recovery: "regenerate",
    }],
  }));

  expect(container.textContent).toContain("The automatic checks found 1 problem. Publishing is blocked.");
  expect(container.textContent).toContain("3 passage(s) still have no explicit audience.");
  expect(container.textContent).toContain("Next step: regenerate the review draft.");
  expect(container.textContent).not.toContain("Publish and play");
  expect(button("Regenerate review draft").disabled).toBe(false);
  expect(inspectAdventureManifest).not.toHaveBeenCalled();
});

it("offers preparation when no draft exists yet", async () => {
  await renderWith(solo({
    state: "no_draft", counts: null, reviewed_digest: null,
    checks: [{ code: "draft_present", status: "failed", message: "No review draft is prepared for this adventure.", count: null, recovery: "regenerate" }],
  }));
  expect(container.textContent).toContain("No review draft is prepared yet.");
  expect(button("Prepare review draft")).toBeTruthy();
});

it("shows the published state without offering a second publication", async () => {
  await renderWith(solo({ state: "published", counts: null, reviewed_digest: null, checks: [] }));
  expect(container.textContent).toContain("Published. Play and the session map use this reviewed version.");
  expect([...container.querySelectorAll("button")].some((b) => b.textContent === "Publish and play")).toBe(false);
});

it("reports a failed publication safely and offers a reload", async () => {
  await renderWith(solo({ checks: passedChecks }));
  vi.mocked(publishAdventureManifest).mockRejectedValue(
    new ApiError(409, JSON.stringify({ detail: { code: "adventure_manifest_review_required", message: "The inspected manifest changed. Inspect it again before publishing." } })),
  );
  await act(async () => { button("Publish and play").click(); });
  expect(container.textContent).not.toContain(SECRET);
  expect(container.textContent).toContain("Reload review");
  expect(button("Reload review").disabled).toBe(false);
});

it("keeps passage text behind the explicit spoiler opt-in, which is recorded and publishes nothing", async () => {
  await renderWith(solo({ checks: passedChecks }));
  vi.mocked(recordSoloSpoilerReview).mockResolvedValue({ recorded: true });
  vi.mocked(inspectAdventureManifest).mockRejectedValue(new ApiError(409, "{}"));

  expect(container.textContent).not.toContain(SECRET);
  const summary = [...container.querySelectorAll("summary")].find((s) => s.textContent === "Review the material anyway (spoilers)")!;
  await act(async () => { summary.click(); });
  await act(async () => { button("Show secret text").click(); });
  expect(recordSoloSpoilerReview).not.toHaveBeenCalled();
  expect(button("Open the full review").disabled).toBe(true);

  const acknowledge = container.querySelector('input[type="checkbox"]') as HTMLInputElement;
  await act(async () => { acknowledge.click(); });
  await act(async () => { button("Open the full review").click(); });

  expect(recordSoloSpoilerReview).toHaveBeenCalledWith("salt_gate", 7);
  expect(publishAdventureManifest).not.toHaveBeenCalled();
  // Only now does the existing passage-level review mount, and it is the manual flow.
  expect(inspectAdventureManifest).toHaveBeenCalled();
  expect(container.textContent).toContain("Needs review.");
});

it("does not open the spoiler review when recording the opt-in fails", async () => {
  await renderWith(solo({ checks: passedChecks }));
  vi.mocked(recordSoloSpoilerReview).mockRejectedValue(new ApiError(500, "{}"));
  const summary = [...container.querySelectorAll("summary")].find((s) => s.textContent === "Review the material anyway (spoilers)")!;
  await act(async () => { summary.click(); });
  await act(async () => { button("Show secret text").click(); });
  await act(async () => { (container.querySelector('input[type="checkbox"]') as HTMLInputElement).click(); });
  await act(async () => { button("Open the full review").click(); });
  expect(inspectAdventureManifest).not.toHaveBeenCalled();
  expect(container.textContent).not.toContain(SECRET);
});

it("keeps the manual review flow for shared campaigns and does not show the solo summary", async () => {
  vi.mocked(getSoloAdventureReview).mockResolvedValue({
    mode: "shared", state: null, title: null, rules_edition: null, checks: [], warnings: [], counts: null, reviewed_digest: null, reviewer: null,
  });
  vi.mocked(inspectAdventureManifest).mockRejectedValue(new ApiError(409, "{}"));
  await act(() => root.render(<AdventureReview campaign={campaign} onUpdated={vi.fn()} />));
  expect(container.textContent).toContain("Needs review.");
  expect(container.textContent).toContain("Only passages you mark as player or planner text are used in play.");
  expect(container.textContent).not.toContain("Publish and play");
  expect(recordSoloSpoilerReview).not.toHaveBeenCalled();
});

it("does not fall back to passage review when the solo decision cannot be loaded", async () => {
  vi.mocked(getSoloAdventureReview).mockRejectedValue(new ApiError(409, JSON.stringify({ detail: { code: "adventure_source_unverified", message: "This adventure has no verified source record, so its review draft cannot be prepared. Start a new campaign with the current preparation pipeline, then remove this one." } })));
  await act(() => root.render(<AdventureReview campaign={campaign} onUpdated={vi.fn()} />));
  expect(container.textContent).toContain("no verified source record");
  expect(inspectAdventureManifest).not.toHaveBeenCalled();
});
