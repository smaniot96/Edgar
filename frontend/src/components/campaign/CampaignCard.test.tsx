// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { cancelAdventure, getExtractionPreview, retryAdventure } from "../../api/client";
import type { AdventureRead, CampaignRead } from "../../api/types";
import { buildCards } from "../../lib/campaignCards";
import { CampaignCard } from "./CampaignCard";

vi.mock("../../dev/ModeContext", () => ({ useMode: () => ({ isDev: false }) }));
vi.mock("../../api/client", async (original) => ({ ...await original<object>(), cancelAdventure: vi.fn(), getExtractionPreview: vi.fn(), retryAdventure: vi.fn() }));

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.clearAllMocks();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => { await act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); });

it("lets a standalone partial module recover using the exact inspected artifact", async () => {
  const ingestion = { source_kind: "pdf" as const, source_sha256: "source-hash", extraction_sha256: "text-hash", page_count: 1, required_pages: [1], chunk_count: 1, quality_passed: true, warning_count: 0, review_note: "Inspect OCR locally.", issues: [] };
  const adventure: AdventureRead = { slug: "bridge", title: "Bridge", description: null, level_range: null, cover_image: null, status: "partial", can_retry: true, extraction_preview_available: true, extraction_preview_current: true, ingestion };
  const card = buildCards([adventure], [], [], 1)[0];
  vi.mocked(getExtractionPreview).mockResolvedValue({ ...ingestion, source_current: true, pages: [], previews: [{ kind: "text", page: 1, page_end: 1, section: "Bridge", text: "The party crosses the bridge." }] });
  vi.mocked(retryAdventure).mockResolvedValue({ ...adventure, status: "processing" });
  const onRetried = vi.fn();
  await act(() => root.render(<CampaignCard card={card} onPlay={vi.fn()} onDelete={vi.fn()} opening={false} deleting={false} onRetried={onRetried} />));
  expect(container.textContent).toContain("Retry retained source");
  expect(container.textContent).not.toContain("Open the campaign");
  await act(async () => {
    const details = container.querySelector("details")!;
    details.open = true;
    details.dispatchEvent(new Event("toggle"));
  });
  const inspected = [...container.querySelectorAll("button")].find((b) => b.textContent === "Index inspected PDF preview")!;
  await act(async () => inspected.click());
  expect(retryAdventure).toHaveBeenCalledWith("bridge", { reviewed_source_sha256: "source-hash", reviewed_extraction_sha256: "text-hash" });
  expect(onRetried).toHaveBeenCalledOnce();
});

it("shows real chapter progress and lets the owner cancel while the published source stays playable", async () => {
  const adventure: AdventureRead = {
    slug: "bridge", title: "Bridge", description: null, level_range: null, cover_image: null,
    status: "ready", can_retry: false,
    readiness: { play_allowed: true, history_allowed: true, state: "ready", primary_action: "play", blockers: [] },
    job: {
      version: 1, job_id: "a".repeat(32), kind: "generation", state: "running",
      stage: "generating_chapters", completed_chapters: 2, total_chapters: 6,
      checkpoint_available: true, retryable: false, retained_work: "outline_and_chapters",
      action_label: "Resume saved chapters", explanation: "The outline and 2 of 6 chapters are saved.",
      source_version: null,
    },
  };
  const card = buildCards([adventure], [], [], 1)[0];
  vi.mocked(cancelAdventure).mockResolvedValue({ ...adventure.job!, state: "cancel_requested" });
  const onRetried = vi.fn();
  await act(() => root.render(<CampaignCard card={card} onPlay={vi.fn()} onDelete={vi.fn()} opening={false} deleting={false} onRetried={onRetried} />));
  expect(container.textContent).toContain("Generating campaign chapters — 2 of 6 saved.");
  expect([...container.querySelectorAll("button")].some((button) => button.textContent?.includes("Start adventure") && !button.disabled)).toBe(true);
  await act(async () => {
    [...container.querySelectorAll("button")].find((button) => button.textContent === "Cancel preparation")!.click();
  });
  expect(cancelAdventure).toHaveBeenCalledWith("bridge");
  expect(onRetried).toHaveBeenCalledOnce();
});

it("does not offer cancellation after validated-source publication begins", async () => {
  const adventure: AdventureRead = {
    slug: "bridge", title: "Bridge", description: null, level_range: null, cover_image: null,
    status: "ready",
    job: {
      version: 1, job_id: "c".repeat(32), kind: "ingestion", state: "running",
      stage: "publishing", completed_chapters: 0, total_chapters: null,
      checkpoint_available: false, retryable: false, retained_work: "source_pdf",
      action_label: null, explanation: null, source_version: "index-v2",
    },
  };
  const card = buildCards([adventure], [], [], 1)[0];
  await act(() => root.render(<CampaignCard card={card} onPlay={vi.fn()} onDelete={vi.fn()} opening={false} deleting={false} />));
  expect(container.textContent).toContain("Finishing publication of the validated source.");
  expect([...container.querySelectorAll("button")].some((button) => button.textContent?.includes("Cancel preparation"))).toBe(false);
});

it("keeps a blocked campaign open for saved history and explains the safe next step", async () => {
  const adventure: AdventureRead = { slug: "bridge", title: "Bridge", description: null, level_range: null, cover_image: null, status: "ready" };
  const campaign: CampaignRead = {
    id: 7, title: "Bridge", system: "5e", created_by: 1, created_at: "2026-10-14",
    adventure_collections: ["bridge"], source_mode: "module", status: "active", ended_at: null,
    retrieval_degraded: false, play_allowed: true, sources: [],
    readiness: { play_allowed: false, history_allowed: true, state: "adventure_review_needed", primary_action: "review_adventure", blockers: [{ code: "adventure_review_needed", message: "Review and publish the adventure before starting or continuing play.", action: "review_adventure" }] },
  };
  const card = buildCards([adventure], [campaign], [], 1)[0];
  const onPlay = vi.fn();
  await act(() => root.render(<CampaignCard card={card} onPlay={onPlay} onDelete={vi.fn()} opening={false} deleting={false} />));
  expect(container.textContent).toContain("Needs attention");
  expect(container.textContent).toContain("Review and publish the adventure before starting or continuing play.");
  const open = [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("View campaign"))!;
  expect(open.disabled).toBe(false);
  await act(() => open.click());
  expect(onPlay).toHaveBeenCalledOnce();
});

it.each([false, true])("never advertises index-only standalone modules as playable (projection present: %s)", async (hasProjection) => {
  const adventure: AdventureRead = {
    slug: "bridge", title: "Bridge", description: null, level_range: null, cover_image: null, status: "ready",
    ...(hasProjection ? { readiness: { play_allowed: false, history_allowed: true, state: "adventure_review_needed", primary_action: "review_adventure", blockers: [{ code: "adventure_review_needed", message: "Review and publish the adventure before starting or continuing play.", action: "review_adventure" }] } } : {}),
  };
  const onPlay = vi.fn();
  const onRetried = vi.fn();
  await act(() => root.render(<CampaignCard card={buildCards([adventure], [], [], 1)[0]} onPlay={onPlay} onDelete={vi.fn()} opening={false} deleting={false} onRetried={onRetried} />));
  expect([...container.querySelectorAll("button")].some((button) => button.textContent?.trim() === "Start adventure")).toBe(false);
  const blocked = [...container.querySelectorAll("button")].find((button) => button.textContent === (hasProjection ? "Needs attention" : "Readiness unknown"))!;
  expect(blocked.disabled).toBe(true);
  await act(() => blocked.click());
  expect(onPlay).not.toHaveBeenCalled();
  if (hasProjection) {
    expect(container.textContent).toContain("Review and publish the adventure before starting or continuing play.");
  } else {
    expect(container.textContent).toContain("Campaign readiness could not be checked.");
    await act(() => [...container.querySelectorAll("button")].find((button) => button.textContent === "Check again")!.click());
    expect(onRetried).toHaveBeenCalledOnce();
  }
});


it("renders the public cover from a nested UI route and preserves the action after image failure", async () => {
  const adventure: AdventureRead = { slug: "bridge", title: "The Bridge", description: null, level_range: null, cover_image: "assets/bridge.svg", status: "ready", readiness: { play_allowed: true, history_allowed: true, state: "ready", primary_action: "play", blockers: [] } };
  vi.stubEnv("BASE_URL", "/ui/");
  window.history.replaceState({}, "", "/ui/campaigns/17");
  const onPlay = vi.fn();
  await act(() => root.render(<CampaignCard card={buildCards([adventure], [], [], 1)[0]} onPlay={onPlay} onDelete={vi.fn()} opening={false} deleting={false} />));
  const image = container.querySelector("img")!;
  expect(image.getAttribute("src")).toBe("/ui/assets/bridge.svg");
  await act(() => image.dispatchEvent(new Event("error")));
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector('[aria-hidden]')?.textContent).toBe("B");
  const play = [...container.querySelectorAll("button")].find((button) => button.textContent?.trim() === "Start adventure")!;
  await act(() => play.click());
  expect(onPlay).toHaveBeenCalledOnce();
  window.history.replaceState({}, "", "/");
  vi.unstubAllEnvs();
});

function countOf(text: string, needle: string) {
  return text.split(needle).length - 1;
}

it("states a failed job's message once on the card, not as both a note and a failure block", async () => {
  const message = "This adventure has no verified source record, so its review draft cannot be prepared. Start a new campaign with the current preparation pipeline, then remove this one.";
  const adventure: AdventureRead = {
    slug: "salt_gate", title: "The Salt Gate", description: null, level_range: null, cover_image: null, status: "failed",
    job: {
      version: 1, job_id: "d".repeat(32), kind: "ingestion", state: "failed",
      stage: "extracting", completed_chapters: 0, total_chapters: null,
      checkpoint_available: false, retryable: false, retained_work: "source_pdf",
      action_label: null, explanation: null, source_version: null,
      failure: { code: "adventure_source_unverified", category: "source", message, recovery_action: "repair_source", retryable: false, support_ref: "ref-1", outcome: "not_committed" },
    },
  };
  await act(() => root.render(<CampaignCard card={buildCards([adventure], [], [], 1)[0]} onPlay={vi.fn()} onDelete={vi.fn()} opening={false} deleting={false} />));
  expect(countOf(container.textContent ?? "", message)).toBe(1);
});

it("states a readiness blocker once when the card's recovery note would repeat it", async () => {
  const adventure: AdventureRead = { slug: "velmoor", title: "Velmoor", description: null, level_range: null, cover_image: null, status: "ready" };
  const campaign: CampaignRead = {
    id: 8, title: "Velmoor", system: "5e", created_by: 1, created_at: "2026-10-14",
    adventure_collections: ["velmoor"], source_mode: "module", status: "active", ended_at: null,
    retrieval_degraded: true, play_allowed: false, sources: [],
    readiness: { play_allowed: false, history_allowed: true, state: "source_missing", primary_action: "recover_source", blockers: [{ code: "source_missing", message: "The bound source is missing.", action: "recover_source" }] },
  };
  await act(() => root.render(<CampaignCard card={buildCards([adventure], [campaign], [], 1)[0]} onPlay={vi.fn()} onDelete={vi.fn()} opening={false} deleting={false} />));
  expect(countOf(container.textContent ?? "", "The bound source is missing.")).toBe(1);
  expect(container.textContent).toContain("Restore the bound adventure source before playing.");
});

it("uses wording that does not assume a separate campaign host in local single-user mode", async () => {
  const adventure: AdventureRead = { slug: "velmoor", title: "Velmoor", description: null, level_range: null, cover_image: null, status: "ready" };
  const campaign: CampaignRead = {
    id: 8, title: "Velmoor", system: "5e", created_by: 1, created_at: "2026-10-14",
    adventure_collections: ["velmoor"], source_mode: "module", status: "active", ended_at: null,
    retrieval_degraded: true, play_allowed: false, sources: [],
    readiness: { play_allowed: false, history_allowed: true, state: "source_missing", primary_action: "recover_source", blockers: [{ code: "source_missing", message: "The bound source is missing.", action: "recover_source" }] },
  };
  await act(() => root.render(<CampaignCard card={buildCards([adventure], [campaign], [], 1)[0]} onPlay={vi.fn()} onDelete={vi.fn()} opening={false} deleting={false} />));
  expect(container.textContent).not.toContain("campaign host");
  expect(container.textContent).not.toContain("Ask the");
});

it("shows Homebrew instead of Ready when a homebrew campaign has no adventure source", async () => {
  const adventure: AdventureRead = { slug: "chalice", title: "Chalice Of The Mountain God", description: null, level_range: null, cover_image: null, status: "missing" };
  const campaign: CampaignRead = {
    id: 9, title: "Chalice Of The Mountain God", system: "5e", created_by: 1, created_at: "2026-10-14",
    adventure_collections: ["chalice"], source_mode: "homebrew", status: "active", ended_at: null,
    retrieval_degraded: false, play_allowed: true, sources: [],
    readiness: { play_allowed: true, history_allowed: true, state: "ready", primary_action: "play", blockers: [] },
  };
  await act(() => root.render(<CampaignCard card={buildCards([adventure], [campaign], [], 1)[0]} onPlay={vi.fn()} onDelete={vi.fn()} opening={false} deleting={false} />));
  const pills = [...container.querySelectorAll("span")].map((span) => span.textContent);
  expect(pills).toContain("Homebrew");
  expect(pills).not.toContain("Ready");
});

it("titles a failed generation without its in-progress placeholder and shows no slug-derived subtitle", async () => {
  const failed: AdventureRead = { slug: "ai-campaign-20261018", title: "AI campaign (generating…)", description: null, level_range: null, cover_image: null, status: "failed" };
  await act(() => root.render(<CampaignCard card={buildCards([failed], [], [], 1)[0]} onPlay={vi.fn()} onDelete={vi.fn()} opening={false} deleting={false} />));
  expect(container.querySelector("h3")?.textContent).toBe("AI campaign");
  expect(container.textContent).not.toContain("generating");

  const module: AdventureRead = { slug: "d_d_5e_lost_mine", title: "D D 5E Lost Mine", description: null, level_range: null, cover_image: null, status: "ready" };
  const campaign: CampaignRead = {
    id: 10, title: "Lost Mine of Phandelver", system: "5e", created_by: 1, created_at: "2026-10-14",
    adventure_collections: ["d_d_5e_lost_mine"], source_mode: "module", status: "active", ended_at: null,
    retrieval_degraded: false, play_allowed: true, sources: [],
    readiness: { play_allowed: true, history_allowed: true, state: "ready", primary_action: "play", blockers: [] },
  };
  await act(() => root.render(<CampaignCard card={buildCards([module], [campaign], [], 1)[0]} onPlay={vi.fn()} onDelete={vi.fn()} opening={false} deleting={false} />));
  expect(container.querySelector("h3")?.textContent).toBe("Lost Mine of Phandelver");
  expect(container.textContent).not.toContain("D D 5E");
});
