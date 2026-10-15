import { useEffect, useState } from "react";

import { cancelAdventure, getCampaign, recoverCampaignSource, retryAdventure, setCampaignSourceMode } from "../../api/client";
import type { CampaignRead } from "../../api/types";
import { isJobActive, isPublicationCommitting, jobFailureHeading, preparationProgress } from "../../lib/adventureJob";
import { Button } from "../ui/Button";
import { ErrorNotice } from "../ui/ErrorNotice";
import { ExtractionPreview } from "./ExtractionPreview";

/** Always visible in player mode when grounding needs recovery or homebrew was chosen. */
export function SourceRecovery({ campaign, onUpdated }: {
  campaign: CampaignRead;
  onUpdated: (campaign: CampaignRead) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const sources = campaign.sources ?? [];
  const processing = sources.some((s) => s.status === "processing" || isJobActive(s.job));
  const failedJob = sources.some((s) => s.job && ["interrupted", "failed", "cancelled"].includes(s.job.state));
  const resumableGeneration = sources.some((s) => s.job?.kind === "generation" && s.job.retryable);
  const healthyHomebrew = campaign.source_mode === "homebrew" && campaign.play_allowed !== false && !processing && !failedJob;
  const needsPdfUpload = sources.some((s) =>
    ["missing", "empty", "partial", "failed"].includes(s.status) &&
    !(s.job?.kind === "generation" && s.job.retryable),
  );
  useEffect(() => {
    if (!processing) return;
    const timer = window.setTimeout(() => {
      getCampaign(campaign.id).then(onUpdated).catch(setError);
    }, 3000);
    return () => window.clearTimeout(timer);
  }, [processing, campaign, onUpdated]);

  if (!campaign.retrieval_degraded && !processing && !failedJob && campaign.source_mode !== "homebrew") return null;

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      onUpdated(await getCampaign(campaign.id));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  if (healthyHomebrew) {
    return (
      <section aria-label="Homebrew source status" className="mx-3 my-2 shrink-0 rounded-control border border-line bg-surface/60 px-3 py-2 text-sm sm:mx-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="font-medium text-ink">Homebrew play selected</p>
          <p className="text-ink-muted">The DM is improvising from your saved story.</p>
          {sources.length > 0 && sources.every((source) => source.status === "ready") ? (
            <Button variant="secondary" size="sm" disabled={busy} onClick={() => void run(() => setCampaignSourceMode(campaign.id, "module"))}>Return to module play</Button>
          ) : null}
        </div>
        <details className="mt-1">
          <summary className="cursor-pointer text-xs font-medium text-ink-muted">Source recovery options</summary>
          <div className="mt-2 space-y-3 border-t border-line pt-2">
            <p className="text-xs text-ink-muted">You can keep playing in homebrew mode while recovering the bound sources. Module play resumes when every source is ready.</p>
            {sources.length === 0 ? <p className="text-xs text-ink-muted">No adventure source is bound to this campaign.</p> : null}
            {sources.map((source) => (
              <div key={source.slug} className="space-y-1 border-t border-line pt-2 text-xs">
                <p className="font-medium text-ink">{source.title} — {source.status}</p>
                {source.message ? <p className="text-ink-muted">{source.message}</p> : null}
                {source.status !== "ready" && source.job?.kind !== "generation" ? (
                  <label className="block space-y-1">
                    <span className="block text-ink-muted">Recover with a PDF</span>
                    <input aria-label={`Recover ${source.title} PDF`} type="file" accept=".pdf,application/pdf" disabled={busy}
                      onChange={(event) => {
                        const file = event.currentTarget.files?.[0];
                        event.currentTarget.value = "";
                        if (file) void run(() => recoverCampaignSource(campaign.id, source.slug, file));
                      }} />
                  </label>
                ) : null}
                {source.status !== "ready" && source.source_pdf_available && !source.job ? (
                  <Button variant="secondary" size="sm" disabled={busy} onClick={() => void run(() => retryAdventure(source.slug))}>Retry retained PDF</Button>
                ) : null}
              </div>
            ))}
          </div>
        </details>
        <ErrorNotice error={error} />
      </section>
    );
  }

  return (
    <section aria-label="Campaign source recovery" className="m-3 space-y-3 rounded-card border border-warning bg-surface-sunken p-4 text-sm">
      <h2 className="font-semibold">{campaign.source_mode === "homebrew" ? "Homebrew play selected" : processing ? "Source preparation in progress" : campaign.retrieval_degraded ? "Adventure sources need recovery" : "Adventure source update needs attention"}</h2>
      <p className="text-ink-muted">
        {campaign.source_mode === "homebrew"
          ? "The DM is improvising from your saved story. Your original source bindings are preserved for recovery."
          : campaign.retrieval_degraded
            ? processing
              ? "Module play is paused while sources are being prepared. Follow their progress below, or choose homebrew play."
              : resumableGeneration && needsPdfUpload
              ? "Module play is paused until every bound source is usable. Resume saved generation where available and provide a PDF for other sources that need recovery, or choose homebrew play."
              : resumableGeneration
                ? "Module play is paused until the generated source is ready. Resume its saved preparation below, or choose homebrew play."
                : "Module play is paused until every bound adventure source is usable. Recover your source PDF, or explicitly choose homebrew play to let the DM improvise."
            : failedJob
              ? "The latest preparation attempt stopped. The last published source remains available for play."
              : "A source update is running while the last published campaign remains available for play."}
      </p>
      {sources.length === 0 ? <p>No adventure source is bound to this campaign.</p> : null}
      {sources.map((source) => (
        <div key={source.slug} className="space-y-2 border-t border-line pt-2">
          <p className="font-medium">{source.title} — {source.status}</p>
          <p className="text-ink-muted">{source.message ?? "The source is ready for module play."}</p>
          {isJobActive(source.job) ? (
            <div role="status" className="space-y-2 rounded-control border border-info/30 bg-info-soft px-3 py-2">
              <p className="font-medium">{preparationProgress(source.job)}</p>
              {source.job?.explanation ? <p className="text-xs text-ink-muted">{source.job.explanation}</p> : null}
              {source.job?.state === "cancel_requested" ? (
                <p className="text-xs text-ink-muted">Cancellation requested…</p>
              ) : isPublicationCommitting(source.job) ? (
                <p className="text-xs text-ink-muted">Finishing publication of the validated source. Cancellation is no longer available at this stage.</p>
              ) : (
                <Button variant="secondary" size="sm" disabled={busy} onClick={() => void run(() => cancelAdventure(source.slug))}>Cancel preparation</Button>
              )}
            </div>
          ) : null}
          {source.job && ["interrupted", "failed", "cancelled"].includes(source.job.state) ? (
            <div role="status" className="space-y-1 rounded-control border border-warning/30 bg-warning-soft px-3 py-2">
              <p className="font-medium">{jobFailureHeading(source.job)}</p>
              {source.job.explanation ? <p className="text-xs text-ink-muted">{source.job.explanation}</p> : null}
              {source.job.retryable ? <Button variant="secondary" size="sm" disabled={busy} onClick={() => void run(() => retryAdventure(source.slug))}>{source.job.action_label ?? "Retry preparation"}</Button> : null}
            </div>
          ) : null}
          {source.extraction_preview_available ? <ExtractionPreview slug={source.slug} onUsePreview={source.extraction_preview_current && (source.job ? source.job.retryable && ["interrupted", "failed", "cancelled"].includes(source.job.state) && source.job.kind === "ingestion" : ["failed", "partial"].includes(source.status) && source.ingestion?.source_kind === "pdf") ? (sourceHash, extractionHash) => run(() => retryAdventure(source.slug, { reviewed_source_sha256: sourceHash, reviewed_extraction_sha256: extractionHash })) : undefined} /> : null}
          {source.status !== "ready" ? (
            <p className="text-xs text-ink-muted">
              {source.job?.kind === "generation"
                ? source.job.explanation ?? "Saved generation work may be resumed without uploading a PDF."
                : source.source_pdf_available
                  ? "A retained source PDF is available."
                  : "The original source PDF is not available here. Recovery requires a PDF you provide."}
            </p>
          ) : null}
          {["missing", "empty", "partial", "failed"].includes(source.status) && !(source.job?.kind === "generation" && source.job.retryable) ? (
            <div className="flex flex-wrap items-center gap-3">
              <label className="space-y-1">
                <span className="block">Re-upload the PDF for {source.title}</span>
                <input aria-label={`Recover ${source.title} PDF`} type="file" accept=".pdf,application/pdf" disabled={busy}
                  onChange={(e) => {
                    const file = e.currentTarget.files?.[0];
                    e.currentTarget.value = "";
                    if (file) void run(() => recoverCampaignSource(campaign.id, source.slug, file));
                  }} />
              </label>
              {source.source_pdf_available && !source.job ? (
                <Button variant="secondary" size="sm" disabled={busy} onClick={() => void run(() => retryAdventure(source.slug))}>Retry retained PDF</Button>
              ) : null}
            </div>
          ) : null}
        </div>
      ))}
      <div className="flex flex-wrap gap-2">
        <Button variant="secondary" size="sm" disabled={busy} onClick={() => void run(async () => {})}>Check sources again</Button>
        {campaign.source_mode !== "homebrew" ? (
          <Button size="sm" disabled={busy} onClick={() => void run(() => setCampaignSourceMode(campaign.id, "homebrew"))}>Continue as homebrew</Button>
        ) : sources.length > 0 && sources.every((s) => s.status === "ready") ? (
          <Button size="sm" disabled={busy} onClick={() => void run(() => setCampaignSourceMode(campaign.id, "module"))}>Return to module play</Button>
        ) : null}
      </div>
      <ErrorNotice error={error} />
    </section>
  );
}
