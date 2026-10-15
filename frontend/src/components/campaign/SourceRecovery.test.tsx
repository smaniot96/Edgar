// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cancelAdventure, getCampaign, recoverCampaignSource, setCampaignSourceMode } from "../../api/client";
import type { CampaignRead } from "../../api/types";
import { SourceRecovery } from "./SourceRecovery";
vi.mock("../../dev/ModeContext", () => ({ useMode: () => ({ isDev: false }) }));
vi.mock("../../api/client", async (original) => ({ ...await original<object>(), cancelAdventure: vi.fn(), getCampaign: vi.fn(), recoverCampaignSource: vi.fn(), setCampaignSourceMode: vi.fn(), retryAdventure: vi.fn() }));
const missing: CampaignRead = {
  id: 2, title: "Lost module", system: "5e", created_by: 1, created_at: "2026-10-14",
  adventure_collections: ["lost_module"], source_mode: "module", status: "active", ended_at: null,
  retrieval_degraded: true, play_allowed: false,
  sources: [{ slug: "lost_module", title: "Lost module", status: "missing", usable_chunks: 0,
    expected_chunks: null, collection_exists: false, source_pdf_available: false,
    message: "The bound adventure collection is missing." }],
};
let container: HTMLDivElement;
let root: Root;
beforeEach(() => { vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true); vi.clearAllMocks(); container = document.createElement("div"); document.body.append(container); root = createRoot(container); });
afterEach(async () => { await act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); });
it("requires the player's explicit fallback click and preserves recovery controls", async () => {
  const updated = { ...missing, source_mode: "homebrew" as const, play_allowed: true };
  vi.mocked(getCampaign).mockResolvedValue(updated); vi.mocked(setCampaignSourceMode).mockResolvedValue(updated);
  const onUpdated = vi.fn();
  await act(() => root.render(<SourceRecovery campaign={missing} onUpdated={onUpdated} />));
  expect(container.textContent).toContain("original source PDF is not available");
  expect(container.textContent).toContain("Module play is paused"); expect(setCampaignSourceMode).not.toHaveBeenCalled();
  const choose = [...container.querySelectorAll("button")].find((b) => b.textContent === "Continue as homebrew")!;
  await act(async () => { choose.click(); });
  expect(setCampaignSourceMode).toHaveBeenCalledWith(2, "homebrew"); expect(onUpdated).toHaveBeenCalledWith(updated);
  await act(() => root.render(<SourceRecovery campaign={updated} onUpdated={onUpdated} />));
  expect(container.textContent).toContain("Homebrew play selected"); expect(container.querySelector('input[aria-label="Recover Lost module PDF"]')).not.toBeNull();
});
it("routes a provided PDF to the intended bound slug", async () => {
  vi.mocked(getCampaign).mockResolvedValue(missing);
  vi.mocked(recoverCampaignSource).mockResolvedValue({ slug: "lost_module", title: "Lost module", description: null, level_range: null, cover_image: null, status: "processing" });
  await act(() => root.render(<SourceRecovery campaign={missing} onUpdated={vi.fn()} />));
  const input = container.querySelector("input")!;
  const file = new File(["%PDF supplied"], "Original.pdf", { type: "application/pdf" });
  Object.defineProperty(input, "files", { value: [file] });
  await act(async () => { input.dispatchEvent(new Event("change", { bubbles: true })); });
  expect(recoverCampaignSource).toHaveBeenCalledWith(2, "lost_module", file); expect(setCampaignSourceMode).not.toHaveBeenCalled();
});

it("shows replacement progress and cancellation while the last ready source remains playable", async () => {
  const updating: CampaignRead = {
    ...missing,
    retrieval_degraded: false,
    play_allowed: true,
    sources: [{
      slug: "lost_module", title: "Lost module", status: "ready", usable_chunks: 4,
      expected_chunks: 4, collection_exists: true, source_pdf_available: false,
      message: "The source is ready for module play.",
      job: {
        version: 1, job_id: "b".repeat(32), kind: "ingestion", state: "running",
        stage: "embedding", completed_chapters: 0, total_chapters: null,
        checkpoint_available: false, retryable: false, retained_work: "source_pdf",
        action_label: null, explanation: "The previous published version remains available.",
        source_version: "index-v2",
      },
    }],
  };
  vi.mocked(getCampaign).mockResolvedValue(updating);
  vi.mocked(cancelAdventure).mockResolvedValue({ ...updating.sources![0].job!, state: "cancel_requested" });
  const onUpdated = vi.fn();
  await act(() => root.render(<SourceRecovery campaign={updating} onUpdated={onUpdated} />));
  expect(container.textContent).toContain("Source preparation in progress");
  expect(container.textContent).toContain("Building the searchable campaign index.");
  expect(container.textContent).toContain("last published campaign remains available for play");
  const cancel = [...container.querySelectorAll("button")].find((button) => button.textContent === "Cancel preparation")!;
  await act(async () => { cancel.click(); });
  expect(cancelAdventure).toHaveBeenCalledWith("lost_module");
  expect(onUpdated).toHaveBeenCalledWith(updating);
});
