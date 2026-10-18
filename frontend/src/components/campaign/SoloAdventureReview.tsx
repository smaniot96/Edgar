import { useState, type ReactNode } from "react";

import {
  compileAdventureManifest,
  getCampaign,
  publishAdventureManifest,
  recordSoloSpoilerReview,
} from "../../api/client";
import type { CampaignRead, SoloAdventureReview as SoloReview } from "../../api/types";
import { Button } from "../ui/Button";
import { ErrorNotice } from "../ui/ErrorNotice";

type Phase = "idle" | "preparing" | "publishing" | "published" | "error";

interface SoloReviewCardProps {
  campaignId: number;
  slug: string;
  title: string;
  review: SoloReview;
  onUpdated: (campaign: CampaignRead) => void;
  onRefresh: () => void;
  /** Renders the passage-level review. Mounted only after the explicit spoiler opt-in. */
  renderSpoilerReview: () => ReactNode;
}

/**
 * Solo host review. Shows only structural results (checks, counts, coverage, warnings and the
 * spoiler-free title). Publication calls the existing publish route with the digest the server
 * computed for the exact draft; the server re-verifies it. Passage text needs a deliberate opt-in.
 */
export function SoloAdventureReview(props: SoloReviewCardProps) {
  const { campaignId, slug, title, review, onUpdated, onRefresh, renderSpoilerReview } = props;
  const [phase, setPhase] = useState<Phase>(review.state === "published" ? "published" : "idle");
  const [error, setError] = useState<unknown>(null);
  const [spoilerStep, setSpoilerStep] = useState<"closed" | "confirming" | "open">("closed");
  const [spoilerAck, setSpoilerAck] = useState(false);
  const [busy, setBusy] = useState(false);

  async function prepareDraft() {
    setBusy(true);
    setError(null);
    setPhase("preparing");
    try {
      await compileAdventureManifest(slug);
      setPhase("idle");
      onRefresh();
    } catch (err) {
      setError(err);
      setPhase("error");
    } finally {
      setBusy(false);
    }
  }

  async function publishAndPlay() {
    if (!review.reviewed_digest || !review.reviewer) return;
    setBusy(true);
    setError(null);
    setPhase("publishing");
    try {
      await publishAdventureManifest(slug, review.reviewed_digest, review.reviewer);
      onUpdated(await getCampaign(campaignId));
      setPhase("published");
    } catch (err) {
      setError(err);
      setPhase("error");
    } finally {
      setBusy(false);
    }
  }

  async function openSpoilerReview() {
    setBusy(true);
    setError(null);
    try {
      await recordSoloSpoilerReview(slug, campaignId);
      setSpoilerStep("open");
    } catch (err) {
      setError(err);
      setPhase("error");
    } finally {
      setBusy(false);
    }
  }

  const failed = review.checks.filter((check) => check.status === "failed");
  const passed = review.checks.filter((check) => check.status === "passed");
  const counts = review.counts;
  const ready = review.state === "ready" && failed.length === 0;

  return (
    <div className="space-y-3 border-t border-line pt-3" aria-label={`Review ${title}`}>
      <p className="font-medium">{title}</p>
      {review.title ? <p className="text-xs text-ink-muted">Adventure: {review.title}</p> : null}

      {review.state === "no_draft" ? (
        <>
          <p role="status" className="text-ink-muted">No review draft is prepared yet. Prepare one from the saved source, then the automatic checks run.</p>
          <Button size="sm" disabled={busy} loading={phase === "preparing"} loadingText="Preparing…" onClick={() => void prepareDraft()}>
            Prepare review draft
          </Button>
        </>
      ) : null}

      {review.state === "blocked" ? (
        <div className="space-y-2">
          <p role="status" className="font-medium text-danger">The automatic checks found {failed.length} problem{failed.length === 1 ? "" : "s"}. Publishing is blocked.</p>
          <ul className="space-y-2 text-xs">
            {failed.map((check) => (
              <li key={check.code} className="rounded-control border border-line bg-surface p-2">
                <p>{check.message}</p>
                {check.recovery === "regenerate" ? <p className="text-ink-muted">Next step: regenerate the review draft.</p> : null}
              </li>
            ))}
          </ul>
          {failed.some((check) => check.recovery === "regenerate") ? (
            <Button variant="secondary" size="sm" disabled={busy} loading={phase === "preparing"} loadingText="Regenerating…" onClick={() => void prepareDraft()}>
              Regenerate review draft
            </Button>
          ) : null}
        </div>
      ) : null}

      {review.state === "needs_classification" ? (
        <div className="space-y-2">
          <p role="status" className="font-medium">This adventure is an upload; its passages need your classification.</p>
          <p className="text-xs text-ink-muted">
            Classification happens only in the full review below, where you read each passage. Nothing changes for players until you publish there.
          </p>
          <ul className="list-disc space-y-1 pl-5 text-xs">
            {failed.map((check) => <li key={check.code}>{check.message}</li>)}
          </ul>
        </div>
      ) : null}

      {review.state === "ready" && phase !== "published" ? (
        <div className="space-y-2">
          <p role="status" className="text-ink-muted">Every automatic check passed. Publish to start play with this adventure.</p>
          <Button size="sm" disabled={busy || !ready} loading={phase === "publishing"} loadingText="Publishing…" onClick={() => void publishAndPlay()}>
            Publish and play
          </Button>
        </div>
      ) : null}

      {phase === "published" ? <p role="status" className="font-medium text-ink">Published. Play and the session map use this reviewed version.</p> : null}

      {counts ? (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-4">
          <dt className="text-ink-muted">Player text</dt><dd>{counts.player}</dd>
          <dt className="text-ink-muted">Planner text</dt><dd>{counts.planner}</dd>
          <dt className="text-ink-muted">Excluded</dt><dd>{counts.excluded}</dd>
          <dt className="text-ink-muted">Unclassified</dt><dd>{counts.unreviewed}</dd>
          <dt className="text-ink-muted">Scenes</dt><dd>{counts.scenes}</dd>
          <dt className="text-ink-muted">Entities</dt><dd>{counts.entities}</dd>
          <dt className="text-ink-muted">Policies</dt><dd>{counts.resolution_policies}</dd>
          <dt className="text-ink-muted">Source coverage</dt><dd>{counts.coverage_percent}%</dd>
        </dl>
      ) : null}

      {passed.length > 0 ? (
        <details>
          <summary className="cursor-pointer text-xs font-medium text-ink-muted">Passed checks ({passed.length})</summary>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-xs">
            {passed.map((check) => <li key={check.code}>{check.message}</li>)}
          </ul>
        </details>
      ) : null}

      {review.warnings.length > 0 ? (
        <ul className="list-disc space-y-1 pl-5 text-xs text-ink-muted">
          {review.warnings.map((warning) => <li key={warning.code}>{warning.message}</li>)}
        </ul>
      ) : null}

      {phase === "error" ? (
        <div className="space-y-2">
          <ErrorNotice error={error} />
          <Button variant="secondary" size="sm" disabled={busy} onClick={onRefresh}>Reload review</Button>
        </div>
      ) : null}

      <details className="rounded-control border border-line bg-surface p-3">
        <summary className="cursor-pointer text-xs font-medium">Review the material anyway (spoilers)</summary>
        <div className="mt-2 space-y-2 text-xs">
          {spoilerStep === "closed" ? (
            <>
              <p className="text-ink-muted">
                This opens the adventure's secret, planner and hidden text. Opening it is logged. It does not change what players see or publish anything: you classify passages and publish in that review.
              </p>
              <Button variant="secondary" size="sm" disabled={busy} onClick={() => setSpoilerStep("confirming")}>Show secret text</Button>
            </>
          ) : null}
          {spoilerStep === "confirming" ? (
            <>
              <label className="flex items-start gap-2">
                <input type="checkbox" checked={spoilerAck} disabled={busy} onChange={(e) => setSpoilerAck(e.currentTarget.checked)} />
                <span>I understand this shows secret and planner text for this adventure.</span>
              </label>
              <Button size="sm" disabled={busy || !spoilerAck} onClick={() => void openSpoilerReview()}>Open the full review</Button>
            </>
          ) : null}
          {spoilerStep === "open" ? renderSpoilerReview() : null}
        </div>
      </details>
    </div>
  );
}
