import { useState } from "react";
import type { FormEvent } from "react";

import {
  ApiError,
  createWorldFlag,
  deleteWorldFlag,
  listWorldFlags,
  updateWorldFlag,
} from "../../api/client";
import type { WorldFlagRead } from "../../api/types";
import { Button, ButtonLink } from "../../components/ui/Button";
import { EmptyState, LoadingText } from "../../components/ui/EmptyState";
import { Input, TextField } from "../../components/ui/Field";
import { cardClasses } from "../../components/ui/styles";
import { useMode } from "../../dev/ModeContext";
import { cn } from "../../lib/cn";
import { useQuery } from "../../lib/useQuery";

type Outcome = { kind: "success" | "error"; message: string } | null;

function safeFailure(action: "load" | "create" | "update" | "delete", error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 0 || error.status === 502 || error.status === 503 || error.status === 504) {
      return "The server could not be reached. Check the connection and try again.";
    }
    if (error.status === 404) return "That flag could not be found. Refresh the list and try again.";
    if (error.status === 409) return "That flag changed while you were editing it. Refresh and try again.";
    if (error.status >= 400 && error.status < 500) {
      return action === "create"
        ? "The flag could not be created. Check the key and value, then try again."
        : "The flag request was rejected. Check the value and try again.";
    }
  }
  return action === "load"
    ? "World overrides could not be loaded. Try again."
    : "The flag change could not be saved. Try again.";
}

function FlagRow({
  flag,
  onSave,
  onDelete,
}: {
  flag: WorldFlagRead;
  onSave: (value: string) => Promise<void>;
  onDelete: () => void;
}) {
  const [val, setVal] = useState(flag.value);
  const [saving, setSaving] = useState(false);
  const inputId = `flag-${flag.id}`;
  return (
    <tr>
      <td className="py-2 pr-4 align-middle">
        <label htmlFor={inputId} className="break-all font-mono text-xs text-ink">
          {flag.key}
        </label>
      </td>
      <td className="py-2 pr-2">
        <Input
          id={inputId}
          maxLength={255}
          value={val}
          onChange={(e) => setVal(e.target.value)}
          className="py-1 text-xs"
        />
      </td>
      <td className="py-2">
        <div className="flex justify-end gap-1">
          {val !== flag.value ? (
            <Button variant="secondary" size="sm" disabled={saving} onClick={() => setVal(flag.value)}>
              Cancel
            </Button>
          ) : null}
          <Button
            variant="secondary"
            size="sm"
            disabled={val === flag.value}
            loading={saving}
            onClick={() => {
              setSaving(true);
              void onSave(val).finally(() => setSaving(false));
            }}
          >
            Save
          </Button>
          <Button
            variant="danger"
            size="sm"
            onClick={onDelete}
            aria-label={`Delete flag ${flag.key}`}
          >
            Delete
          </Button>
        </div>
      </td>
    </tr>
  );
}

export default function WorldTab({ campaignId }: { campaignId: number }) {
  const { isDev } = useMode();
  // Manual flags are private owner controls. Do not fetch their raw payload for a hidden
  // ordinary-mode editor; the session journal is the player-facing record of earned facts.
  const flags = useQuery(isDev ? `world-flags:${campaignId}` : null, (s) => listWorldFlags(campaignId, s));
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome>(null);

  async function add(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setOutcome(null);
    try {
      await createWorldFlag(campaignId, { key: key.trim(), value: value.trim() });
      setKey("");
      setValue("");
      flags.refetch();
      setOutcome({ kind: "success", message: "World flag created." });
    } catch (err) {
      setOutcome({ kind: "error", message: safeFailure("create", err) });
    } finally {
      setBusy(false);
    }
  }

  async function save(flag: WorldFlagRead, next: string) {
    setOutcome(null);
    try {
      await updateWorldFlag(campaignId, flag.key, next);
      flags.refetch();
      setOutcome({ kind: "success", message: "World flag updated." });
    } catch (err) {
      setOutcome({ kind: "error", message: safeFailure("update", err) });
    }
  }

  async function remove(flag: WorldFlagRead) {
    if (!window.confirm(`Delete flag "${flag.key}"?`)) return;
    setOutcome(null);
    try {
      await deleteWorldFlag(campaignId, flag.key);
      flags.refetch();
      setOutcome({ kind: "success", message: "World flag deleted." });
    } catch (err) {
      setOutcome({ kind: "error", message: safeFailure("delete", err) });
    }
  }

  if (!isDev) {
    return (
      <section aria-labelledby="world-state-heading" className="space-y-3">
        <h2 id="world-state-heading" className="font-serif text-lg font-semibold text-ink">
          Known story changes
        </h2>
        <p className="max-w-prose text-sm leading-relaxed text-ink-muted">
          The campaign flag editor is an advanced owner tool. Choose a saved session to read the
          committed discoveries and changes recorded for that playthrough.
        </p>
        <ButtonLink to={`/campaigns/${campaignId}?tab=sessions`} variant="secondary" size="sm">
          Choose a session for its Journal
        </ButtonLink>
      </section>
    );
  }

  return (
    <div className="space-y-4">
      <p className="rounded-control bg-warning-soft px-3 py-2 text-sm text-warning">
        Advanced world overrides for Campaign {campaignId}. Saving edits legacy campaign flags used
        by the DM. These owner actions do not rewrite committed journal facts, inventory, promises,
        or history.
      </p>
      <form onSubmit={(e) => void add(e)} className={cn(cardClasses, "space-y-3 p-4")}>
        <h3 className="text-sm font-semibold">Advanced world overrides</h3>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <TextField
            fieldClassName="flex-1"
            label="Key"
            required
            maxLength={255}
            placeholder="bridge_destroyed"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            className="font-mono"
          />
          <TextField
            fieldClassName="flex-1"
            label="Value"
            required
            maxLength={255}
            placeholder="true"
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
          <Button type="submit" loading={busy} loadingText="Saving…">
            Set
          </Button>
        </div>
      </form>
      {outcome ? (
        <p
          className={outcome.kind === "success" ? "text-sm text-success" : "text-sm text-danger"}
          role={outcome.kind === "error" ? "alert" : "status"}
          aria-live={outcome.kind === "error" ? "assertive" : "polite"}
        >
          {outcome.message}
        </p>
      ) : null}
      {flags.error && flags.data === undefined ? (
        <div role="alert" className="space-y-2 text-sm text-danger">
          <p>{safeFailure("load", flags.error)}</p>
          <Button variant="secondary" size="sm" onClick={flags.refetch}>Retry</Button>
        </div>
      ) : flags.data === undefined ? (
        <LoadingText />
      ) : flags.data.length === 0 ? (
        <EmptyState title="No world flags yet">
          No manual flags are stored for this campaign. This does not mean the story is unchanged;
          committed discoveries belong to each saved session's journal.
        </EmptyState>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-ink-muted">
                <th scope="col" className="pb-2 pr-4 font-medium">
                  Key
                </th>
                <th scope="col" className="pb-2 pr-4 font-medium">
                  Value
                </th>
                <th scope="col" className="pb-2">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {flags.data.map((f) => (
                <FlagRow
                  // Re-key on value so a refetch resets the local draft.
                  key={`${f.key}\u0000${f.value}`}
                  flag={f}
                  onSave={(v) => save(f, v)}
                  onDelete={() => void remove(f)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
