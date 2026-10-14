import { useEffect, useState } from "react";

import { getCampaign, recoverCampaignSource, retryAdventure, setCampaignSourceMode } from "../../api/client";
import type { CampaignRead } from "../../api/types";
import { Button } from "../ui/Button";
import { ErrorNotice } from "../ui/ErrorNotice";

/** Always visible in player mode when grounding needs recovery or homebrew was chosen. */
export function SourceRecovery({ campaign, onUpdated }: {
  campaign: CampaignRead;
  onUpdated: (campaign: CampaignRead) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const sources = campaign.sources ?? [];
  const processing = sources.some((s) => s.status === "processing");
  useEffect(() => {
    if (!processing) return;
    const timer = window.setTimeout(() => {
      getCampaign(campaign.id).then(onUpdated).catch(setError);
    }, 3000);
    return () => window.clearTimeout(timer);
  }, [processing, campaign, onUpdated]);

  if (!campaign.retrieval_degraded && campaign.source_mode !== "homebrew") return null;

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

  return (
    <section aria-label="Campaign source recovery" className="m-3 space-y-3 rounded-card border border-warning bg-surface-sunken p-4 text-sm">
      <h2 className="font-semibold">{campaign.source_mode === "homebrew" ? "Homebrew play selected" : "Adventure sources need recovery"}</h2>
      <p className="text-ink-muted">
        {campaign.source_mode === "homebrew"
          ? "The DM is improvising from your saved story. Your original source bindings are preserved for recovery."
          : "Module play is paused until every bound adventure source is usable. Recover your source PDF, or explicitly choose homebrew play to let the DM improvise."}
      </p>
      {sources.length === 0 ? <p>No adventure source is bound to this campaign.</p> : null}
      {sources.map((source) => (
        <div key={source.slug} className="space-y-2 border-t border-line pt-2">
          <p className="font-medium">{source.title} — {source.status}</p>
          <p className="text-ink-muted">{source.message ?? "The source is ready for module play."}</p>
          {source.status !== "ready" ? (
            <p className="text-xs text-ink-muted">
              {source.source_pdf_available ? "A retained source PDF is available." : "The original source PDF is not available here. Recovery requires a PDF you provide."}
            </p>
          ) : null}
          {["missing", "empty", "partial", "failed"].includes(source.status) ? (
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
              {source.source_pdf_available ? (
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
