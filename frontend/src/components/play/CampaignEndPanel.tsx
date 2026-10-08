import { useState } from "react";

import { apiFetch, errorMessage } from "../../api/client";
import { Button, ButtonLink } from "../ui/Button";

/** Replaces the composer once the campaign is over, with ways forward. */
export function CampaignEndPanel({
  campaignId,
  onReopened,
}: {
  campaignId: number | null;
  onReopened: () => void;
}) {
  const [reopening, setReopening] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function reopen() {
    if (campaignId == null) return;
    setReopening(true);
    setError(null);
    try {
      await apiFetch(`/api/campaigns/${campaignId}/reopen`, { method: "POST" });
      onReopened();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setReopening(false);
    }
  }

  return (
    <section
      aria-label="Campaign complete"
      className="shrink-0 border-t border-ember-900/60 bg-gradient-to-t from-ember-950/40 to-canvas px-4 pt-4 pb-[max(1rem,env(safe-area-inset-bottom))]"
    >
      <div className="mx-auto flex w-full max-w-3xl flex-col items-center gap-3 text-center">
        <div>
          <h2 className="font-display text-display-sm text-ember-200">🏆 Campaign complete</h2>
          <p className="text-sm text-ink-muted">The tale is told. Well played, adventurer.</p>
        </div>
        <div className="flex flex-wrap justify-center gap-2">
          {campaignId != null ? (
            <ButtonLink to={`/campaigns/${campaignId}`}>Back to campaign</ButtonLink>
          ) : null}
          <ButtonLink to="/campaigns" variant="secondary">
            Start a new adventure
          </ButtonLink>
          {campaignId != null ? (
            <Button
              variant="ghost"
              onClick={() => void reopen()}
              loading={reopening}
              loadingText="Reopening…"
            >
              Reopen campaign
            </Button>
          ) : null}
        </div>
        {error ? (
          <p role="alert" className="text-sm text-danger">
            Couldn't reopen: {error}
          </p>
        ) : null}
      </div>
    </section>
  );
}
