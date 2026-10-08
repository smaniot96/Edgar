/**
 * Character creation form, seeded from /api/character-presets. Used on the Characters page and
 * inline in a campaign's "Start a new session" picker.
 */

import { useState } from "react";
import type { FormEvent } from "react";

import { createCharacter, listCharacterPresets } from "../api/client";
import type { CharacterPreset, CharacterRead } from "../api/types";
import { cn } from "../lib/cn";
import { useQuery } from "../lib/useQuery";
import { Button } from "./ui/Button";
import { ErrorNotice } from "./ui/ErrorNotice";
import { LoadingText } from "./ui/EmptyState";
import { TextField } from "./ui/Field";
import { cardClasses } from "./ui/styles";

export function CharacterForm({
  onCreated,
  onCancel,
  title = "Create character",
  submitLabel = "Create character",
  className,
}: {
  onCreated: (character: CharacterRead) => void;
  onCancel?: () => void;
  title?: string;
  submitLabel?: string;
  className?: string;
}) {
  const presets = useQuery("character-presets", (signal) => listCharacterPresets(signal));
  // `null` = follow the first preset until the player picks one.
  const [pickedKey, setPickedKey] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [charClass, setCharClass] = useState<string | null>(null);
  const [hpText, setHpText] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [hpError, setHpError] = useState<string | null>(null);

  const list = presets.data ?? [];
  const selected: CharacterPreset | undefined =
    list.find((p) => p.key === pickedKey) ?? list[0];
  // Class/HP default to the selected preset until the player edits them.
  const classValue = charClass ?? selected?.character_class ?? "";
  const hpValue = hpText ?? String(selected?.hp_max ?? 10);

  function pick(p: CharacterPreset) {
    setPickedKey(p.key);
    setCharClass(null);
    setHpText(null);
    setHpError(null);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    const hp = Number.parseInt(hpValue, 10);
    if (!Number.isFinite(hp) || hp < 1) {
      setHpError("Enter a whole number of at least 1.");
      return;
    }
    setBusy(true);
    setError(null);
    setHpError(null);
    try {
      const created = await createCharacter({
        name: name.trim(),
        character_class: classValue.trim(),
        level: selected?.level ?? 1,
        hp_max: hp,
        base_stats: selected?.stats ?? {},
        base_inventory: selected?.inventory ?? {},
      });
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

      {presets.pending ? (
        <LoadingText>Loading presets…</LoadingText>
      ) : presets.error ? (
        <ErrorNotice error={presets.error} onRetry={presets.refetch} />
      ) : list.length > 0 ? (
        <fieldset>
          <legend className="mb-1.5 text-xs font-medium text-ink-muted">Starting preset</legend>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {list.map((p) => {
              const active = selected?.key === p.key;
              return (
                <button
                  key={p.key}
                  type="button"
                  aria-pressed={active}
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
        onChange={(e) => setName(e.target.value)}
      />
      <div className="flex gap-3">
        <TextField
          fieldClassName="flex-1"
          label="Class"
          required
          maxLength={255}
          value={classValue}
          onChange={(e) => setCharClass(e.target.value)}
        />
        <TextField
          fieldClassName="w-28"
          label="Max HP"
          type="number"
          inputMode="numeric"
          min={1}
          required
          value={hpValue}
          error={hpError}
          onChange={(e) => {
            setHpText(e.target.value);
            setHpError(null);
          }}
        />
      </div>
      <ErrorNotice error={error} />
      <Button type="submit" loading={busy} loadingText="Creating…">
        {submitLabel}
      </Button>
    </form>
  );
}
