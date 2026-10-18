// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { getExtractionPreview } from "../../api/client";
import { downloadAuthorized } from "../../api/resources";
import { ExtractionPreview } from "./ExtractionPreview";

vi.mock("../../dev/ModeContext", () => ({ useMode: () => ({ isDev: false }) }));
vi.mock("../../api/client", async (original) => ({ ...await original<object>(), getExtractionPreview: vi.fn() }));
vi.mock("../../api/resources", async (original) => ({ ...await original<object>(), downloadAuthorized: vi.fn().mockResolvedValue(undefined) }));

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
  sessionStorage.clear();
  vi.unstubAllGlobals();
});

it("shows recoverable quality details and literal unreviewed scene text locally", async () => {
  sessionStorage.setItem("edgar.apiToken", "edgar_extraction_fixture_token");
  vi.mocked(getExtractionPreview).mockResolvedValue({
    source_sha256: "approved-source", extraction_sha256: "selected-text", page_count: 2,
    source_current: false,
    required_pages: [1, 2], chunk_count: 2, quality_passed: false, warning_count: 1,
    review_note: "Scene candidates are not reviewed openings.",
    issues: [{ code: "extraction_quality", page: 2, message: "duplicate_word_layer" }],
    pages: [{ page: 2, method: "layout_ocr", quality: { errors: ["duplicate_word_layer"] }, warnings: ["OCR numbers need review"] }],
    previews: [{ kind: "scene_candidate", page: 1, page_end: 1, section: "Bridge", text: "<script>do not execute</script> The bridge." }],
  });
  await act(() => root.render(<ExtractionPreview slug="the_bridge" />));
  expect(getExtractionPreview).not.toHaveBeenCalled();
  await act(async () => {
    const details = container.querySelector("details")!;
    details.open = true;
    details.dispatchEvent(new Event("toggle"));
  });
  expect(getExtractionPreview).toHaveBeenCalledWith("the_bridge");
  expect(container.textContent).toContain("Extraction needs recovery");
  expect(container.textContent).toContain("This preview describes a previous source");
  expect(container.textContent).toContain("Page 2: duplicate_word_layer");
  expect(container.textContent).toContain("Scene candidate (unreviewed)");
  expect(container.textContent).toContain("<script>do not execute</script>");
  expect(container.querySelector("script")).toBeNull();
  // With a token the download goes through the authorized client (EDG-160).
  expect(container.querySelector("a")).toBeNull();
  const download = [...container.querySelectorAll("button")].find((b) => b.textContent === "Download full extracted Markdown")!;
  await act(async () => { download.click(); });
  expect(downloadAuthorized).toHaveBeenCalledWith("/api/adventures/the_bridge/extraction/markdown", "the_bridge-extracted.md");
});
