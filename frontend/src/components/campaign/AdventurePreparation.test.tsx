// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import {
  cancelAdventure,
  getExtractionPreview,
  retryAdventure,
} from "../../api/client";
import type { AdventureRead } from "../../api/types";

import { AdventurePreparation } from "./AdventurePreparation";

vi.mock("../../dev/ModeContext", () => ({ useMode: () => ({ isDev: false }) }));
vi.mock("../../api/client", async (original) => ({
  ...(await original<object>()),
  cancelAdventure: vi.fn(),
  getExtractionPreview: vi.fn(),
  retryAdventure: vi.fn(),
}));

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

it("lets a standalone partial module recover using the exact inspected artifact", async () => {
  const ingestion = {
    source_kind: "pdf" as const,
    source_sha256: "source-hash",
    extraction_sha256: "text-hash",
    page_count: 1,
    required_pages: [1],
    chunk_count: 1,
    quality_passed: true,
    warning_count: 0,
    review_note: "Inspect OCR locally.",
    issues: [],
  };
  const adventure: AdventureRead = {
    slug: "bridge",
    title: "Bridge",
    description: null,
    level_range: null,
    cover_image: null,
    status: "partial",
    can_retry: true,
    extraction_preview_available: true,
    extraction_preview_current: true,
    ingestion,
  };
  vi.mocked(getExtractionPreview).mockResolvedValue({
    ...ingestion,
    source_current: true,
    pages: [],
    previews: [
      {
        kind: "text",
        page: 1,
        page_end: 1,
        section: "Bridge",
        text: "The party crosses the bridge.",
      },
    ],
  });
  vi.mocked(retryAdventure).mockResolvedValue({
    ...adventure,
    status: "processing",
  });
  const onRetried = vi.fn();
  await act(() =>
    root.render(
      <AdventurePreparation adventure={adventure} onUpdated={onRetried} />,
    ),
  );
  expect(container.textContent).toContain("Retry preparation");
  expect(container.textContent).not.toContain("Open the campaign");
  await act(async () => {
    const details = container.querySelector("details")!;
    details.open = true;
    details.dispatchEvent(new Event("toggle"));
  });
  const inspected = [...container.querySelectorAll("button")].find(
    (b) => b.textContent === "Index inspected PDF preview",
  )!;
  await act(async () => inspected.click());
  expect(retryAdventure).toHaveBeenCalledWith("bridge", {
    reviewed_source_sha256: "source-hash",
    reviewed_extraction_sha256: "text-hash",
  });
  expect(onRetried).toHaveBeenCalledOnce();
});

it("shows real chapter progress and lets the owner cancel while the published source stays playable", async () => {
  const adventure: AdventureRead = {
    slug: "bridge",
    title: "Bridge",
    description: null,
    level_range: null,
    cover_image: null,
    status: "ready",
    can_retry: false,
    readiness: {
      play_allowed: true,
      history_allowed: true,
      state: "ready",
      primary_action: "play",
      blockers: [],
    },
    job: {
      version: 1,
      job_id: "a".repeat(32),
      kind: "generation",
      state: "running",
      stage: "generating_chapters",
      completed_chapters: 2,
      total_chapters: 6,
      checkpoint_available: true,
      retryable: false,
      retained_work: "outline_and_chapters",
      action_label: "Resume saved chapters",
      explanation: "The outline and 2 of 6 chapters are saved.",
      source_version: null,
    },
  };
  vi.mocked(cancelAdventure).mockResolvedValue({
    ...adventure.job!,
    state: "cancel_requested",
  });
  const onRetried = vi.fn();
  await act(() =>
    root.render(
      <AdventurePreparation adventure={adventure} onUpdated={onRetried} />,
    ),
  );
  expect(container.textContent).toContain(
    "Generating campaign chapters — 2 of 6 saved.",
  );
  await act(async () => {
    [...container.querySelectorAll("button")]
      .find((button) => button.textContent === "Cancel preparation")!
      .click();
  });
  expect(cancelAdventure).toHaveBeenCalledWith("bridge");
  expect(onRetried).toHaveBeenCalledOnce();
});

it("does not offer cancellation after validated-source publication begins", async () => {
  const adventure: AdventureRead = {
    slug: "bridge",
    title: "Bridge",
    description: null,
    level_range: null,
    cover_image: null,
    status: "ready",
    job: {
      version: 1,
      job_id: "c".repeat(32),
      kind: "ingestion",
      state: "running",
      stage: "publishing",
      completed_chapters: 0,
      total_chapters: null,
      checkpoint_available: false,
      retryable: false,
      retained_work: "source_pdf",
      action_label: null,
      explanation: null,
      source_version: "index-v2",
    },
  };
  await act(() =>
    root.render(
      <AdventurePreparation adventure={adventure} onUpdated={vi.fn()} />,
    ),
  );
  expect(container.textContent).toContain(
    "Publishing the validated campaign source",
  );
  expect(
    [...container.querySelectorAll("button")].some((button) =>
      button.textContent?.includes("Cancel preparation"),
    ),
  ).toBe(false);
});
