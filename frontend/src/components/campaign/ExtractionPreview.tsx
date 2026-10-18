import { useState } from "react";

import { apiBase, getExtractionPreview } from "../../api/client";
import { downloadAuthorized } from "../../api/resources";
import type { ExtractionPreview as Preview } from "../../api/types";
import { ErrorNotice } from "../ui/ErrorNotice";
import { Button } from "../ui/Button";

/** Source text is displayed literally; PDF content never becomes executable markup. */
export function ExtractionPreview({ slug, onUsePreview }: { slug: string; onUsePreview?: (sourceHash: string, extractionHash: string) => Promise<void> }) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const [indexing, setIndexing] = useState(false);

  async function indexPreview() {
    if (!preview || !onUsePreview) return;
    setIndexing(true);
    setError(null);
    try {
      await onUsePreview(preview.source_sha256, preview.extraction_sha256);
    } catch (e) {
      setError(e);
    } finally {
      setIndexing(false);
    }
  }

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setPreview(await getExtractionPreview(slug));
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }

  return (
    <details className="mb-3 text-sm" onToggle={(e) => { if (e.currentTarget.open && !preview && !loading) void load(); }}>
      <summary className="cursor-pointer text-ink-muted">Inspect extraction preview</summary>
      {loading ? <p>Loading preview…</p> : null}
      {preview ? <div className="mt-2 space-y-2">
        {preview.source_current === false ? <p className="text-warning">This preview describes a previous source. Retry extraction to inspect the current source.</p> : null}
        <p>{preview.page_count} source pages · {preview.chunk_count} chunks · {preview.quality_passed ? "Extraction checks passed" : "Extraction needs recovery"}</p>
        <p className="text-xs text-ink-muted">{preview.review_note}</p>
        {preview.issues.map((issue, i) => <p key={i} className="text-danger">{issue.page ? `Page ${issue.page}: ` : ""}{issue.message}</p>)}
        {preview.pages.filter((p) => p.warnings.length).map((p) => <p key={p.page} className="text-xs text-warning">Page {p.page}: {(p.warning_messages ?? p.warnings).join("; ")}</p>)}
        {preview.previews.map((sample) => <div key={sample.kind}>
          <p className="font-medium">{sample.kind === "scene_candidate" ? "Scene candidate (unreviewed)" : sample.kind === "stat" ? "Stat preview" : "Text preview"} · page {sample.page}</p>
          <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded-control bg-surface-sunken p-2 text-xs">{sample.text}</pre>
        </div>)}
        <button
          type="button"
          className="block text-left underline"
          onClick={() => {
            setError(null);
            downloadAuthorized(`${apiBase}/api/adventures/${encodeURIComponent(slug)}/extraction/markdown`, `${slug}-extracted.md`).catch((e: unknown) => setError(e));
          }}
        >
          Download full extracted Markdown
        </button>
        {onUsePreview && preview.source_current && preview.quality_passed ? <div className="space-y-2">
          <p className="text-xs text-ink-muted">After inspecting the full extracted text, use this exact preview to replace the retained PDF's old index.</p>
          <Button size="sm" variant="secondary" loading={indexing} onClick={() => void indexPreview()}>Index inspected PDF preview</Button>
        </div> : null}
        <details className="text-xs text-ink-muted"><summary>Source identity</summary><p className="break-all">SHA256: {preview.source_sha256}</p></details>
      </div> : null}
      <ErrorNotice error={error} onRetry={() => void load()} />
    </details>
  );
}
