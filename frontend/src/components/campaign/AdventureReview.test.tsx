// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  ApiError,
  compileAdventureManifest,
  getCampaign,
  inspectAdventureManifest,
  publishAdventureManifest,
  saveAdventureManifest,
} from "../../api/client";
import type { CampaignRead, ManifestInspection } from "../../api/types";
import { AdventureReview } from "./AdventureReview";

vi.mock("../../api/client", async (original) => ({
  ...await original<object>(),
  inspectAdventureManifest: vi.fn(),
  compileAdventureManifest: vi.fn(),
  saveAdventureManifest: vi.fn(),
  publishAdventureManifest: vi.fn(),
  getCampaign: vi.fn(),
}));

const blocked: CampaignRead = {
  id: 7, title: "Salt Gate", system: "5e", created_by: 1, created_at: "2026-10-17",
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

function inspection(overrides: Partial<ManifestInspection["manifest"]> = {}, passages = [
  { passage_id: "p1", text: "The keeper waits at the dock." },
  { passage_id: "p2", text: "The bell rings at dusk." },
]): ManifestInspection {
  const manifest = {
    source: "salt_gate",
    opening_scene_id: null,
    passages: passages.map((p) => ({ passage_id: p.passage_id, audience: "unreviewed" })),
    ...overrides,
  } as ManifestInspection["manifest"];
  return { manifest, reviewed_digest: "a".repeat(64), passages, review_note: "Inspect every span." };
}

const classified = (value: ManifestInspection): ManifestInspection["manifest"] => ({
  ...value.manifest,
  passages: value.manifest.passages.map((p) => ({ ...p, audience: "player" })),
});

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

function field(label: string): HTMLTextAreaElement | HTMLInputElement {
  return container.querySelector(`[aria-label="${label}"]`) as HTMLTextAreaElement | HTMLInputElement;
}

it("offers a review draft when no draft has been compiled yet", async () => {
  vi.mocked(inspectAdventureManifest).mockRejectedValue(new ApiError(409, JSON.stringify({ detail: { code: "adventure_manifest_review_required", message: "Compile a manifest draft before inspecting or publishing it." } })));
  await act(() => root.render(<AdventureReview campaign={blocked} onUpdated={vi.fn()} />));
  expect(container.textContent).toContain("Needs review.");
  expect(container.textContent).not.toContain("Compile a manifest draft");
  expect(container.textContent).not.toContain("Publish reviewed adventure");
});

it("blocks publication until every passage is classified, the draft is saved, and the review is confirmed", async () => {
  const draft = inspection();
  const ready: ManifestInspection = { ...inspection(), manifest: { ...classified(draft), opening_scene_id: "dock" } as ManifestInspection["manifest"] };
  vi.mocked(inspectAdventureManifest).mockRejectedValue(new ApiError(409, "{}"));
  vi.mocked(compileAdventureManifest).mockResolvedValue(draft);
  vi.mocked(saveAdventureManifest).mockResolvedValue(ready);
  vi.mocked(publishAdventureManifest).mockResolvedValue({ status: "ready", chunks: 2 });
  vi.mocked(getCampaign).mockResolvedValue({ ...blocked, readiness: { ...blocked.readiness!, play_allowed: true, blockers: [] } });
  const onUpdated = vi.fn();

  await act(() => root.render(<AdventureReview campaign={blocked} onUpdated={onUpdated} />));
  await act(async () => { button("Prepare review draft").click(); });
  expect(compileAdventureManifest).toHaveBeenCalledWith("salt_gate");
  expect(container.textContent).toContain("2 passages still marked unreviewed");
  expect(button("Publish reviewed adventure").disabled).toBe(true);

  // Classify every passage in the editable draft, then save it: this changes the digest.
  const edited = JSON.stringify(classified(draft), null, 2);
  await act(async () => {
    const textarea = field("Review draft JSON for Salt Gate") as HTMLTextAreaElement;
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
    setter.call(textarea, edited);
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  });
  expect(button("Save draft edits").disabled).toBe(false);
  await act(async () => { button("Save draft edits").click(); });
  expect(saveAdventureManifest).toHaveBeenCalledWith("salt_gate", expect.objectContaining({ source: "salt_gate" }));
  expect(button("Publish reviewed adventure").disabled).toBe(true); // reviewer and confirmation still missing

  const confirm = container.querySelector('input[type="checkbox"]') as HTMLInputElement;
  await act(async () => { confirm.click(); });
  expect(button("Publish reviewed adventure").disabled).toBe(true); // reviewer name still missing
  await act(async () => {
    const reviewer = field("Reviewer name for Salt Gate") as HTMLInputElement;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    setter.call(reviewer, "Pietro");
    reviewer.dispatchEvent(new Event("input", { bubbles: true }));
  });
  expect(button("Publish reviewed adventure").disabled).toBe(false);

  await act(async () => { button("Publish reviewed adventure").click(); });
  expect(publishAdventureManifest).toHaveBeenCalledWith("salt_gate", ready.reviewed_digest, "Pietro");
  expect(getCampaign).toHaveBeenCalledWith(7);
  expect(onUpdated).toHaveBeenCalled();
  expect(container.textContent).toContain("Published. Play and the session map use this reviewed version.");
});

it("keeps publication disabled while any passage remains unreviewed", async () => {
  vi.mocked(inspectAdventureManifest).mockResolvedValue(inspection({ opening_scene_id: "dock" }, [{ passage_id: "p1", text: "Keeper." }]));
  await act(() => root.render(<AdventureReview campaign={blocked} onUpdated={vi.fn()} />));
  await act(async () => {
    const reviewer = field("Reviewer name for Salt Gate") as HTMLInputElement;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    setter.call(reviewer, "Pietro");
    reviewer.dispatchEvent(new Event("input", { bubbles: true }));
  });
  expect(button("Publish reviewed adventure").disabled).toBe(true);
  expect(container.textContent).toContain("1 passage still marked unreviewed");
  expect(publishAdventureManifest).not.toHaveBeenCalled();
});

it("shows the fixed safe message and a reload recovery when the server refuses publication", async () => {
  const draft = inspection({ opening_scene_id: "dock" }, [{ passage_id: "p1", text: "Keeper." }]);
  const reviewed: ManifestInspection = { ...draft, manifest: classified(draft) as ManifestInspection["manifest"] };
  vi.mocked(inspectAdventureManifest).mockResolvedValueOnce(reviewed).mockResolvedValueOnce(reviewed);
  vi.mocked(publishAdventureManifest).mockRejectedValue(new ApiError(409, JSON.stringify({ detail: { code: "adventure_manifest_review_required", message: "The inspected manifest changed. Inspect it again before publishing." } })));
  await act(() => root.render(<AdventureReview campaign={blocked} onUpdated={vi.fn()} />));
  const confirm = container.querySelector('input[type="checkbox"]') as HTMLInputElement;
  await act(async () => { confirm.click(); });
  await act(async () => {
    const reviewer = field("Reviewer name for Salt Gate") as HTMLInputElement;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    setter.call(reviewer, "Pietro");
    reviewer.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => { button("Publish reviewed adventure").click(); });
  expect(publishAdventureManifest).toHaveBeenCalledWith("salt_gate", reviewed.reviewed_digest, "Pietro");
  // Only the fixed taxonomy copy is shown; the server's free-form detail is never rendered.
  expect(container.textContent).toContain("Review the current adventure material before continuing.");
  expect(container.textContent).not.toContain("inspected manifest changed");
  expect(getCampaign).not.toHaveBeenCalled();

  await act(async () => { button("Reload saved draft").click(); });
  expect(inspectAdventureManifest).toHaveBeenCalledTimes(2);
  expect(container.textContent).toContain("Every passage is classified.");
});

it("reports invalid draft JSON without calling the server", async () => {
  vi.mocked(inspectAdventureManifest).mockResolvedValue(inspection());
  await act(() => root.render(<AdventureReview campaign={blocked} onUpdated={vi.fn()} />));
  await act(async () => {
    const textarea = field("Review draft JSON for Salt Gate") as HTMLTextAreaElement;
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
    setter.call(textarea, "{ not json");
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => { button("Save draft edits").click(); });
  expect(saveAdventureManifest).not.toHaveBeenCalled();
  expect(container.textContent).toContain("not valid JSON");
});

it("hides review-only server internals and never shows raw error text", async () => {
  vi.mocked(inspectAdventureManifest).mockRejectedValue(new ApiError(404, JSON.stringify({ detail: "Not found" })));
  await act(() => root.render(<AdventureReview campaign={blocked} onUpdated={vi.fn()} />));
  expect(container.textContent).not.toContain("passages for host review");
  expect(container.textContent).not.toContain("Publish reviewed adventure");
  expect(button("Reload saved draft")).toBeTruthy();
});

it("explains an unverified source once and offers no draft or reload loop", async () => {
  vi.mocked(inspectAdventureManifest).mockRejectedValue(new ApiError(409, JSON.stringify({ detail: { code: "adventure_source_unverified", message: "Source kind is unverified. Recover its canonical extraction first." } })));
  await act(() => root.render(<AdventureReview campaign={blocked} onUpdated={vi.fn()} />));
  expect(container.textContent).toContain("no verified source record");
  expect(container.textContent).not.toContain("Needs review.");
  expect(container.textContent).not.toContain("Recover its canonical extraction");
  expect([...container.querySelectorAll("button")].map((b) => b.textContent)).toEqual([]);
});
