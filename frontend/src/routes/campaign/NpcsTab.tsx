import { useState } from "react";
import type { FormEvent } from "react";

import { createNpc, deleteNpc, listNpcs } from "../../api/client";
import type { NPCRead } from "../../api/types";
import { Pill } from "../../components/Pill";
import { Button } from "../../components/ui/Button";
import { EmptyState, LoadingText } from "../../components/ui/EmptyState";
import { ErrorNotice } from "../../components/ui/ErrorNotice";
import { TextField } from "../../components/ui/Field";
import { cardClasses } from "../../components/ui/styles";
import { useMode } from "../../dev/ModeContext";
import { cn } from "../../lib/cn";
import { useQuery } from "../../lib/useQuery";

export default function NpcsTab({ campaignId }: { campaignId: number }) {
  const { isDev } = useMode();
  const npcs = useQuery(`npcs:${campaignId}`, (s) => listNpcs(campaignId, s));
  const [name, setName] = useState("");
  const [disposition, setDisposition] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function add(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await createNpc(campaignId, { name: name.trim(), disposition: disposition.trim() });
      setName("");
      setDisposition("");
      npcs.refetch();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  async function remove(n: NPCRead) {
    if (!window.confirm(`Delete ${n.name}?`)) return;
    setError(null);
    try {
      await deleteNpc(n.id);
      npcs.refetch();
    } catch (err) {
      setError(err);
    }
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-ink-muted">
        People the DM tracks in this campaign. Edgar adds them as you meet them; you can add your
        own too.
      </p>
      <form onSubmit={(e) => void add(e)} className={cn(cardClasses, "space-y-3 p-4")}>
        <h3 className="text-sm font-semibold">Add NPC</h3>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <TextField
            fieldClassName="flex-1"
            label="Name"
            required
            maxLength={255}
            placeholder="Sildar Hallwinter"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <TextField
            fieldClassName="flex-1"
            label="Disposition"
            required
            maxLength={255}
            placeholder="friendly, hostile, wary…"
            value={disposition}
            onChange={(e) => setDisposition(e.target.value)}
          />
          <Button type="submit" loading={busy} loadingText="Adding…">
            Add
          </Button>
        </div>
      </form>
      <ErrorNotice error={error} />
      {npcs.error && npcs.data === undefined ? (
        <ErrorNotice error={npcs.error} onRetry={npcs.refetch} />
      ) : npcs.data === undefined ? (
        <LoadingText />
      ) : npcs.data.length === 0 ? (
        <EmptyState title="No NPCs yet">They'll appear here as your story unfolds.</EmptyState>
      ) : (
        <ul className="space-y-2">
          {npcs.data.map((n) => (
            <li
              key={n.id}
              className={cn(cardClasses, "flex items-center justify-between gap-3 px-4 py-3")}
            >
              <div className="min-w-0">
                <p className="truncate text-sm font-medium">
                  {n.name}
                  {isDev ? (
                    <span className="ml-2 font-mono text-xs text-ink-subtle">#{n.id}</span>
                  ) : null}
                </p>
                <Pill className="mt-1">{n.disposition}</Pill>
              </div>
              <Button
                variant="danger"
                size="sm"
                onClick={() => void remove(n)}
                aria-label={`Delete ${n.name}`}
              >
                Delete
              </Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
