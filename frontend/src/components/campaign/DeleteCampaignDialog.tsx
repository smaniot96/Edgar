import { useState } from "react";

import type { SessionRead } from "../../api/types";
import { deletedMessage, deleteTarget, plural, savesText, type DeleteTarget } from "../../lib/campaignDelete";
import { ConfirmDialog } from "../ui/ConfirmDialog";

/**
 * Confirmation for deleting a campaign (with its saves and any adventure source nothing else
 * uses) or, for a card without a campaign, the adventure source alone. Stays open while the
 * delete runs and shows a failure inside; `onDeleted` receives the summary to announce.
 */
export function DeleteCampaignDialog({
  target,
  sessions,
  onClose,
  onDeleted,
}: {
  target: DeleteTarget | null;
  sessions: SessionRead[] | undefined;
  onClose: () => void;
  onDeleted: (message: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  function close() {
    setError(null);
    onClose();
  }

  async function confirm() {
    if (!target) return;
    setBusy(true);
    setError(null);
    try {
      const outcomes = await deleteTarget(target);
      onDeleted(deletedMessage(target, outcomes));
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  const adventureOnly = target != null && target.campaigns.length === 0;
  const liveSources = target?.sources.filter((s) => !s.missing) ?? [];
  const ended = target?.campaigns.filter((c) => c.status === "ended").length ?? 0;

  return (
    <ConfirmDialog
      open={target != null}
      title={adventureOnly ? `Delete the adventure “${target.name}”?` : `Delete “${target?.name ?? ""}”?`}
      confirmLabel={adventureOnly ? "Delete adventure" : "Delete campaign"}
      busyLabel="Deleting…"
      busy={busy}
      error={error}
      onConfirm={() => void confirm()}
      onCancel={close}
    >
      {!target ? null : adventureOnly ? (
        <p>
          This permanently deletes the adventure source, including its uploaded file and everything
          prepared from it. No campaign of yours uses it yet.
        </p>
      ) : (
        <>
          <p>
            {target.campaigns.length === 1
              ? `This permanently deletes the campaign “${target.name}” and ${savesText(target.campaigns, sessions)}.`
              : `This permanently deletes ${plural(target.campaigns.length, "campaign")} of “${target.name}”${ended ? ` (${ended} ended)` : ""} and ${savesText(target.campaigns, sessions)}.`}
          </p>
          {liveSources.map((source) => (
            <p key={source.slug}>
              The adventure source “{source.title}” is deleted too, unless something still needs
              it (another campaign, or saved games of a prepared version). You will see which.
            </p>
          ))}
          <p>Your characters stay in your library.</p>
        </>
      )}
      <p className="font-medium text-ink">This cannot be undone.</p>
    </ConfirmDialog>
  );
}
