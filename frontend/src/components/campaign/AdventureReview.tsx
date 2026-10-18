import { useEffect, useState } from "react";

import {
  ApiError,
  compileAdventureManifest,
  getCampaign,
  getSoloAdventureReview,
  inspectAdventureManifest,
  publishAdventureManifest,
  saveAdventureManifest,
} from "../../api/client";
import { safeFailureFromUnknown } from "../../api/turnStream";
import type { CampaignRead, ManifestInspection, SoloAdventureReview as SoloReview } from "../../api/types";
import { Button } from "../ui/Button";
import { ErrorNotice } from "../ui/ErrorNotice";
import { SoloAdventureReview } from "./SoloAdventureReview";

type Phase = "loading" | "needs_draft" | "draft" | "publishing" | "published" | "error";

/** An adventure without a verified source record can never get a draft, so retrying cannot help. */
function isUnverifiedSource(error: unknown): boolean {
  return safeFailureFromUnknown(error).code === "adventure_source_unverified";
}

interface ReviewCardProps {
  campaignId: number;
  slug: string;
  title: string;
  onUpdated: (campaign: CampaignRead) => void;
}

/**
 * Host-only review of a legacy adventure. Compilation only drafts candidates; every passage
 * must be classified and the exact inspected draft acknowledged before publication.
 */
export function AdventureReview({ campaign, onUpdated }: {
  campaign: CampaignRead;
  onUpdated: (campaign: CampaignRead) => void;
}) {
  const ready = (campaign.sources ?? []).filter((source) => source.status === "ready");
  const targets = ready.length > 0
    ? ready.map((source) => ({ slug: source.slug, title: source.title }))
    : campaign.adventure_collections.map((slug) => ({ slug, title: slug }));
  return (
    <section aria-label="Adventure review" className="mt-3 space-y-3 rounded-card border border-warning bg-surface-sunken p-4 text-sm [overflow-wrap:anywhere] [&_button]:min-h-11">
      <h2 className="font-semibold">Review and publish the adventure</h2>
      <p className="text-ink-muted">Players cannot start or continue play until the host publishes a reviewed version.</p>
      {targets.length === 0 ? <p className="text-ink-muted">No adventure source is bound to this campaign.</p> : null}
      {targets.map((target) => (
        <ReviewTarget key={target.slug} campaignId={campaign.id} slug={target.slug} title={target.title} onUpdated={onUpdated} />
      ))}
    </section>
  );
}

type TargetLoad = { status: "loading" } | { status: "error"; error: unknown } | { status: "ready"; review: SoloReview };

/**
 * Chooses the review flow from the server's decision. A solo host gets the spoiler-safe summary;
 * a shared campaign keeps the manual passage review unchanged.
 */
function ReviewTarget({ campaignId, slug, title, onUpdated }: ReviewCardProps) {
  const [load, setLoad] = useState<TargetLoad>({ status: "loading" });
  const [version, setVersion] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    getSoloAdventureReview(slug, campaignId, controller.signal)
      .then((review) => {
        if (!controller.signal.aborted) setLoad({ status: "ready", review });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setLoad({ status: "error", error });
      });
    return () => controller.abort();
  }, [slug, campaignId, version]);

  function reload() {
    setLoad({ status: "loading" });
    setVersion((value) => value + 1);
  }

  if (load.status === "loading") {
    return (
      <div className="space-y-2 border-t border-line pt-3" aria-label={`Review ${title}`}>
        <p className="font-medium">{title}</p>
        <p role="status" className="text-ink-muted">Checking the review draft…</p>
      </div>
    );
  }
  if (load.status === "error") {
    return (
      <div className="space-y-2 border-t border-line pt-3" aria-label={`Review ${title}`}>
        <p className="font-medium">{title}</p>
        <ErrorNotice error={load.error} />
        {isUnverifiedSource(load.error) ? null : (
          <Button variant="secondary" size="sm" onClick={reload}>Reload review</Button>
        )}
      </div>
    );
  }
  if (load.review.mode === "shared") {
    return <ReviewCard campaignId={campaignId} slug={slug} title={title} onUpdated={onUpdated} />;
  }
  return (
    <SoloAdventureReview
      campaignId={campaignId}
      slug={slug}
      title={title}
      review={load.review}
      onUpdated={onUpdated}
      onRefresh={reload}
      renderSpoilerReview={() => <ReviewCard campaignId={campaignId} slug={slug} title={title} onUpdated={onUpdated} />}
    />
  );
}

function ReviewCard({ campaignId, slug, title, onUpdated }: ReviewCardProps) {
  const [phase, setPhase] = useState<Phase>("loading");
  const [inspection, setInspection] = useState<ManifestInspection | null>(null);
  const [jsonText, setJsonText] = useState("");
  const [savedText, setSavedText] = useState("");
  const [reviewer, setReviewer] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);

  /** Fetches the saved draft; state changes happen only in the request callbacks. */
  function fetchDraft(signal?: AbortSignal) {
    inspectAdventureManifest(slug, signal)
      .then((value) => {
        if (signal?.aborted) return;
        adoptInspection(value);
      })
      .catch((err: unknown) => {
        if (signal?.aborted) return;
        // 409 means no draft has been compiled yet; any other failure is shown safely.
        if (err instanceof ApiError && err.status === 409 && !isUnverifiedSource(err)) setPhase("needs_draft");
        else {
          setError(err);
          setPhase("error");
        }
      });
  }

  function reloadDraft() {
    setPhase("loading");
    setError(null);
    fetchDraft();
  }

  useEffect(() => {
    const controller = new AbortController();
    fetchDraft(controller.signal);
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  function adoptInspection(value: ManifestInspection) {
    const text = JSON.stringify(value.manifest, null, 2);
    setInspection(value);
    setJsonText(text);
    setSavedText(text);
    setConfirmed(false);
    setPhase("draft");
  }

  async function compile() {
    setBusy(true);
    setError(null);
    try {
      adoptInspection(await compileAdventureManifest(slug));
    } catch (err) {
      setError(err);
      setPhase("error");
    } finally {
      setBusy(false);
    }
  }

  async function saveDraft() {
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(jsonText) as Record<string, unknown>;
    } catch {
      setNotice("The review draft is not valid JSON. Correct it before saving.");
      return;
    }
    setNotice(null);
    setBusy(true);
    setError(null);
    try {
      adoptInspection(await saveAdventureManifest(slug, parsed));
    } catch (err) {
      setError(err);
      setPhase("error");
    } finally {
      setBusy(false);
    }
  }

  async function publish() {
    if (!inspection) return;
    setPhase("publishing");
    setBusy(true);
    setError(null);
    try {
      await publishAdventureManifest(slug, inspection.reviewed_digest, reviewer.trim());
      onUpdated(await getCampaign(campaignId));
      setPhase("published");
    } catch (err) {
      setError(err);
      setPhase("error");
    } finally {
      setBusy(false);
    }
  }

  const dirty = jsonText !== savedText;
  const unreviewed = inspection
    ? inspection.manifest.passages.filter((passage) => passage.audience === "unreviewed").length
    : 0;
  const canPublish = !busy && inspection !== null && !dirty && unreviewed === 0 && confirmed && reviewer.trim().length > 0;

  return (
    <div className="space-y-2 border-t border-line pt-3" aria-label={`Review ${title}`}>
      <p className="font-medium">{title}</p>
      <p className="text-xs text-ink-muted">Only passages you mark as player or planner text are used in play.</p>
      {phase === "loading" ? <p role="status" className="text-ink-muted">Checking the review draft…</p> : null}
      {phase === "needs_draft" ? (
        <>
          <p role="status" className="text-ink-muted">Needs review. Prepare a review draft from the saved source, then classify every passage before publishing.</p>
          <Button size="sm" disabled={busy} onClick={() => void compile()}>Prepare review draft</Button>
        </>
      ) : null}
      {phase === "draft" && inspection ? (
        <>
          <p role="status" className="text-ink-muted">
            {unreviewed > 0
              ? `${unreviewed} passage${unreviewed === 1 ? "" : "s"} still marked unreviewed. Set each to player, planner or excluded before publishing.`
              : dirty
                ? "Save the draft edits before publishing."
                : "Every passage is classified. Confirm the review to publish."}
          </p>
          <p className="text-xs text-ink-muted">{inspection.review_note}</p>
          <details>
            <summary className="cursor-pointer text-xs font-medium text-ink-muted">Source passages for host review ({inspection.passages.length})</summary>
            <ol className="mt-2 max-h-72 list-decimal space-y-2 overflow-auto pl-5 text-xs">
              {inspection.passages.map((passage) => (
                <li key={passage.passage_id}><span className="font-mono text-ink-muted">{passage.passage_id}</span><p>{passage.text}</p></li>
              ))}
            </ol>
          </details>
          <label className="block space-y-1 text-xs">
            <span className="block font-medium">Review draft (JSON)</span>
            <textarea aria-label={`Review draft JSON for ${title}`} className="min-h-40 w-full rounded-control border border-line bg-surface p-2 font-mono text-xs"
              value={jsonText} disabled={busy} onChange={(e) => { setJsonText(e.currentTarget.value); setConfirmed(false); setNotice(null); }} />
          </label>
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="secondary" size="sm" disabled={busy || !dirty} onClick={() => void saveDraft()}>Save draft edits</Button>
          </div>
          {notice ? <p role="alert" className="text-xs text-danger">{notice}</p> : null}
          <label className="flex items-start gap-2 text-xs">
            <input type="checkbox" checked={confirmed} disabled={busy || dirty || unreviewed > 0} onChange={(e) => setConfirmed(e.currentTarget.checked)} />
            <span>I have inspected every passage, scene, link and discovery in this draft.</span>
          </label>
          <label className="block space-y-1 text-xs">
            <span className="block font-medium">Reviewer name</span>
            <input aria-label={`Reviewer name for ${title}`} className="min-h-11 w-full max-w-sm rounded-control border border-line bg-surface px-2" value={reviewer} disabled={busy} maxLength={200}
              onChange={(e) => setReviewer(e.currentTarget.value)} />
          </label>
          <Button size="sm" disabled={!canPublish} onClick={() => void publish()}>Publish reviewed adventure</Button>
        </>
      ) : null}
      {phase === "publishing" ? <p role="status" className="text-ink-muted">Publishing the reviewed adventure…</p> : null}
      {phase === "published" ? <p role="status" className="font-medium text-ink">Published. Play and the session map use this reviewed version.</p> : null}
      {phase === "error" ? (
        <div className="space-y-2">
          <ErrorNotice error={error} />
          {isUnverifiedSource(error) ? null : (
            <>
              <p className="text-xs text-ink-muted">Reload the saved draft to check its current state before trying again.</p>
              <Button variant="secondary" size="sm" disabled={busy} onClick={reloadDraft}>Reload saved draft</Button>
            </>
          )}
        </div>
      ) : null}
    </div>
  );
}
