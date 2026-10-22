/**
 * Character creation form, seeded from /api/character-presets. Used on the Characters page and
 * inline in a campaign's "Start a new session" picker.
 */

import { useState } from "react";
import type { FormEvent } from "react";

import { ApiError, createCharacter, listCharacterPresets, type CharacterCreateBody } from "../api/client";
import type { CharacterPreset, CharacterRead } from "../api/types";
import type { CharacterDraft, PendingCharacterCreate } from "../lib/campaignSetup";
import { cn } from "../lib/cn";
import { useQuery } from "../lib/useQuery";
import { Button } from "./ui/Button";
import { ErrorNotice } from "./ui/ErrorNotice";
import { LoadingText } from "./ui/EmptyState";
import { TextField } from "./ui/Field";
import { cardClasses } from "./ui/styles";

/** Readable starting kit, e.g. "longsword, shield, chain mail". Booleans show as the item's name; "none" is omitted. */
function describeKit(inventory: Record<string, unknown>): string {
  const parts = Object.entries(inventory).flatMap(([key, value]) => {
    if (Array.isArray(value)) return value.map(String);
    if (value === true) return [key.replace(/_/g, " ")];
    if (value === false || value == null || value === "none") return [];
    if (typeof value === "number") return [`${value} ${key.replace(/_/g, " ")}`];
    return [String(value)];
  });
  return parts.length > 0 ? `Kit: ${parts.join(", ")}` : "Kit: none";
}

export function CharacterForm({
  onCreated,
  onCancel,
  title = "Create character",
  submitLabel = "Create character",
  className,
  initialDraft,
  pendingCreate,
  onDraftChange,
  onCreateRequested,
  onEditPendingCreate,
  initialLevel = 1,
}: {
  onCreated: (character: CharacterRead) => void;
  onCancel?: () => void;
  title?: string;
  submitLabel?: string;
  className?: string;
  initialDraft?: CharacterDraft;
  /** An unresolved request is replayed with its original key and exact body. */
  pendingCreate?: PendingCharacterCreate | null;
  onDraftChange?: (draft: CharacterDraft) => void;
  /** Called synchronously before the API request so setup can durably retain its retry key. */
  onCreateRequested?: (request: PendingCharacterCreate) => boolean | void;
  onEditPendingCreate?: () => void;
  initialLevel?: number;
}) {
  const [level, setLevel] = useState(() => {
    const saved = pendingCreate?.body.level ?? Number(initialDraft?.presetKey?.match(/_l([1-5])$/)?.[1] ?? initialLevel);
    return saved >= 1 && saved <= 5 ? saved : 1;
  });
  const presets = useQuery(`character-presets:${level}`, (signal) => listCharacterPresets(signal, level));
  // `null` = follow the first preset until the player picks one.
  const [pickedKey, setPickedKey] = useState<string | null>(initialDraft?.presetKey ?? null);
  const [name, setName] = useState(initialDraft?.name ?? "");
  const [hpText, setHpText] = useState<string | null>(initialDraft?.hpText ?? null);
  const [busy, setBusy] = useState(false);
  const [localPending, setLocalPending] = useState<PendingCharacterCreate | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [storageError, setStorageError] = useState<string | null>(null);
  const [hpError, setHpError] = useState<string | null>(null);

  const list = presets.data ?? [];
  // The class is the picked preset's class, so it always comes with that preset's stats and kit.
  // A draft saved with a class that has no preset (for example "Bard") falls back to its preset when
  // one matches, and otherwise to the first preset.
  const selected: CharacterPreset | undefined =
    list.find((p) => p.key === pickedKey) ??
    list.find((p) => p.character_class === initialDraft?.characterClass) ??
    list[0];
  const classValue = selected?.character_class ?? "";
  // HP defaults to the selected preset until the player edits it.
  const hpValue = hpText ?? String(selected?.hp_max ?? 10);
  const retainedCreate = pendingCreate ?? (onCreateRequested ? null : localPending);
  const locked = retainedCreate != null;
  const safeToEditAfterRejection = error instanceof ApiError && [400, 422].includes(error.status);

  function saveDraft(next: Partial<CharacterDraft>) {
    onDraftChange?.({
      name: next.name ?? name,
      presetKey: next.presetKey === undefined ? pickedKey : next.presetKey,
      characterClass: next.characterClass === undefined ? (selected?.character_class ?? null) : next.characterClass,
      hpText: next.hpText === undefined ? hpText : next.hpText,
    });
  }

  // Picking a class resets HP to that preset's value; stats and kit follow from the preset at submit.
  function pick(p: CharacterPreset) {
    setPickedKey(p.key);
    setHpText(null);
    setHpError(null);
    saveDraft({ presetKey: p.key, characterClass: p.character_class, hpText: null });
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!retainedCreate && !selected) return;
    const hp = Number.parseInt(hpValue, 10);
    if (!Number.isFinite(hp) || hp < 1) {
      setHpError("Enter a whole number of at least 1.");
      return;
    }
    setBusy(true);
    setError(null);
    setStorageError(null);
    setHpError(null);
    try {
      const body: CharacterCreateBody = retainedCreate?.body ?? {
        name: name.trim(),
        character_class: classValue,
        level: selected?.level ?? 1,
        hp_max: hp,
        base_stats: selected?.stats ?? {},
        base_inventory: selected?.inventory ?? {},
      };
      const requestId = retainedCreate?.requestId ?? crypto.randomUUID();
      if (!onCreateRequested) setLocalPending({ requestId, body });
      if (onCreateRequested?.({ requestId, body }) === false) {
        setStorageError("Browser storage could not retain the retry details. Allow storage and try again. No hero was created.");
        return;
      }
      const created = await createCharacter(body, requestId);
      setLocalPending(null);
      setName("");
      onCreated(created);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)} className={cn(cardClasses, "space-y-4 p-4", className)}>
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">{title}</h3>
        {onCancel ? (
          <Button variant="ghost" size="sm" onClick={onCancel}>
            Cancel
          </Button>
        ) : null}
      </div>

      <label className="block space-y-1 text-sm">
        <span className="font-medium text-ink-muted">Starting level</span>
        <select aria-label="Starting level" value={level} disabled={locked || busy}
          className="min-h-11 rounded-control border border-line bg-surface-sunken px-3"
          onChange={(event) => {
            const nextLevel = Number(event.target.value);
            const nextKey = selected ? `${selected.character_class.toLowerCase()}_l${nextLevel}` : `fighter_l${nextLevel}`;
            setLevel(nextLevel);
            setPickedKey(nextKey);
            setHpText(null);
            setHpError(null);
            saveDraft({ presetKey: nextKey, hpText: null });
          }}>
          {[1, 2, 3, 4, 5].map((value) => <option key={value} value={value}>Level {value}</option>)}
        </select>
        <span className="block text-xs text-ink-muted">HP and abilities follow the chosen level. Level 4 includes +2 to your class’s main ability.</span>
      </label>

      {presets.pending ? (
        <LoadingText>Loading presets…</LoadingText>
      ) : presets.error ? (
        <ErrorNotice error={presets.error} onRetry={presets.refetch} />
      ) : list.length > 0 ? (
        <fieldset>
          <legend className="mb-1.5 text-xs font-medium text-ink-muted">Class (starting preset)</legend>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {list.map((p) => {
              const active = selected?.key === p.key;
              return (
                <button
                  key={p.key}
                  type="button"
                  aria-pressed={active}
                  disabled={locked}
                  onClick={() => pick(p)}
                  className={cn(
                    "rounded-card border p-3 text-left text-sm transition-colors",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-400",
                    active
                      ? "border-ember-500 bg-ember-950/40"
                      : "border-line bg-surface-sunken hover:border-ember-700",
                  )}
                >
                  <p className="font-medium">{p.label}</p>
                  <p className="mt-1 text-xs text-ink-muted">
                    Level {p.level} {p.character_class} · HP {p.hp_max}
                  </p>
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    {Object.entries(p.stats).map(([k, v]) => (
                      <span
                        key={k}
                        className="rounded bg-surface-raised px-1.5 py-0.5 text-xs text-ink-muted"
                      >
                        {k.toUpperCase()} {v}
                      </span>
                    ))}
                  </div>
                  <p className="mt-1.5 text-xs text-ink-muted">{describeKit(p.inventory)}</p>
                  <p className="mt-1 text-xs text-ink-muted">{p.engine_support}</p>
                </button>
              );
            })}
          </div>
        </fieldset>
      ) : null}

      <TextField
        label="Character name"
        required
        maxLength={255}
        placeholder="e.g. Brannoc the Bold"
        value={name}
        disabled={locked}
        onChange={(e) => {
          setName(e.target.value);
          saveDraft({ name: e.target.value });
        }}
      />
      <div className="flex items-end gap-3">
        <p className="flex-1 text-sm">
          <span className="block text-xs font-medium text-ink-muted">Class</span>
          <span className="font-medium">{classValue || "Choose a preset"}</span>
        </p>
        <TextField
          fieldClassName="w-28"
          label="Max HP"
          type="number"
          inputMode="numeric"
          min={1}
          required
          value={hpValue}
          disabled={locked}
          error={hpError}
          onChange={(e) => {
            setHpText(e.target.value);
            setHpError(null);
            saveDraft({ hpText: e.target.value });
          }}
        />
      </div>
      <ErrorNotice error={error} />
      {storageError ? <p role="alert" className="text-sm text-warning">{storageError}</p> : null}
      {locked ? (
        <p className="text-xs text-ink-muted">
          This hero request is retained. Retry uses the same details, so a completed creation is reused.
        </p>
      ) : null}
      {locked && safeToEditAfterRejection ? (
        <Button type="button" variant="ghost" size="sm" onClick={() => { setError(null); setLocalPending(null); onEditPendingCreate?.(); }}>
          Edit details after validation error
        </Button>
      ) : null}
      <Button type="submit" disabled={!locked && !selected} loading={busy} loadingText="Creating…">
        {locked ? "Retry saved hero creation" : submitLabel}
      </Button>
    </form>
  );
}
