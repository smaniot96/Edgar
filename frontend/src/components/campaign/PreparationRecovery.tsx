import { useState } from "react";

import { completePreparationSetup } from "../../api/client";
import { usePreparationSetup } from "../../routes/home/PreparationFields";
import { Button } from "../ui/Button";
import { ErrorNotice } from "../ui/ErrorNotice";
import { Field } from "../ui/Field";

export function PreparationRecovery({ slug, edition, onStarted }: {
  slug: string; edition?: string; onStarted?: () => void;
}) {
  const preparation = usePreparationSetup();
  const [rulesEdition, setRulesEdition] = useState<"2014" | "2024">(edition === "2024" ? "2024" : "2014");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  return <form className="mb-3 space-y-3" onSubmit={async (event) => {
    event.preventDefault();
    if (!preparation.setup || busy) return;
    setBusy(true); setError(null);
    try {
      await completePreparationSetup(slug, rulesEdition, preparation.setup);
      onStarted?.();
    } catch (cause) { setError(cause); }
    finally { setBusy(false); }
  }}>
    <p className="text-sm text-ink-muted">Complete your setup to start private preparation.</p>
    <Field label="Rules edition">{(props) => <select {...props} value={rulesEdition}
      onChange={(event) => setRulesEdition(event.target.value as "2014" | "2024")}
      className="min-h-11 rounded-control border border-line bg-surface p-2 text-sm">
      <option value="2014">D&amp;D 5e (2014)</option><option value="2024">D&amp;D 5e (2024)</option>
    </select>}</Field>
    {preparation.fields}
    <ErrorNotice error={error} />
    <Button type="submit" className="min-h-11" disabled={!preparation.setup} loading={busy}>Start private preparation</Button>
  </form>;
}
