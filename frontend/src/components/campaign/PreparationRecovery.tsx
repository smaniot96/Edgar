import { useState } from "react";

import { completePreparationSetup } from "../../api/client";
import { usePreparationSetup } from "../../routes/home/PreparationFields";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { Field } from "../ui/Field";

/** Setup for a private preparation that has not started yet, kept out of the card's own layout. */
export function PreparationRecovery({ open, slug, edition, onClose, onStarted }: {
  open: boolean; slug: string; edition?: string; onClose: () => void; onStarted?: () => void;
}) {
  const preparation = usePreparationSetup();
  const [rulesEdition, setRulesEdition] = useState<"2014" | "2024">(edition === "2024" ? "2024" : "2014");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  function close() {
    setError(null);
    onClose();
  }

  async function start() {
    if (!preparation.setup || busy) return;
    setBusy(true);
    setError(null);
    try {
      await completePreparationSetup(slug, rulesEdition, preparation.setup);
      onClose();
      onStarted?.();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <ConfirmDialog
      open={open}
      role="dialog"
      variant="primary"
      title="Set up private preparation"
      confirmLabel="Start private preparation"
      busyLabel="Starting…"
      confirmDisabled={!preparation.setup}
      busy={busy}
      error={error}
      onConfirm={() => void start()}
      onCancel={close}
    >
      <div className="space-y-3">
        <Field label="Rules edition">{(props) => <select {...props} value={rulesEdition}
          onChange={(event) => setRulesEdition(event.target.value as "2014" | "2024")}
          className="min-h-11 rounded-control border border-line bg-surface p-2 text-sm">
          <option value="2014">D&amp;D 5e (2014)</option><option value="2024">D&amp;D 5e (2024)</option>
        </select>}</Field>
        {preparation.fields}
      </div>
    </ConfirmDialog>
  );
}
