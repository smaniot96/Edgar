/**
 * Characters (PG) library.
 *
 * Lists all of the user's characters with name/class/level/HP and which
 * campaign each is currently engaged in (or "Available"). Includes a create
 * form seeded from /api/character-presets, plus delete.
 */

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import type { CharacterListItem, CharacterPreset } from "../api/types";
import {
  createCharacter,
  deleteCharacter,
  errorMessage,
  listCharacterPresets,
  listCharacters,
} from "../api/client";
import { Pill } from "../components/Pill";

function CreateCharacterForm({ onCreated }: { onCreated: () => void }) {
  const [presets, setPresets] = useState<CharacterPreset[] | null>(null);
  const [selected, setSelected] = useState<CharacterPreset | null>(null);
  const [name, setName] = useState("");
  const [charClass, setCharClass] = useState("");
  const [hpMax, setHpMax] = useState(10);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const applyPreset = useCallback((p: CharacterPreset) => {
    setSelected(p);
    setCharClass(p.character_class);
    setHpMax(p.hp_max);
  }, []);

  useEffect(() => {
    listCharacterPresets()
      .then((ps) => {
        setPresets(ps);
        if (ps.length > 0) applyPreset(ps[0]);
      })
      .catch((e) => setError(errorMessage(e)));
  }, [applyPreset]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await createCharacter({
        name: name.trim(),
        character_class: charClass,
        level: selected?.level ?? 1,
        hp_max: hpMax,
        base_stats: selected?.stats ?? {},
        base_inventory: selected?.inventory ?? {},
      });
      setName("");
      onCreated();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      onSubmit={(e) => void submit(e)}
      className="space-y-4 rounded-lg border border-[#2a2c30] bg-[#1a1c20] p-4"
    >
      <h3 className="text-sm font-semibold">Create character</h3>
      {presets === null ? (
        <p className="text-sm text-[#9ca3af]">Loading presets…</p>
      ) : presets.length > 0 ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {presets.map((p) => (
            <button
              key={p.key}
              type="button"
              onClick={() => applyPreset(p)}
              className={`rounded-lg border p-3 text-left text-sm transition-colors ${
                selected?.key === p.key
                  ? "border-[#3b82f6] bg-[#1f3a5f]"
                  : "border-[#2a2c30] bg-[#101216] hover:border-[#3b82f6]"
              }`}
            >
              <p className="font-medium">{p.label}</p>
              <p className="mt-1 text-xs text-[#9ca3af]">
                L{p.level} {p.character_class} · HP {p.hp_max}
              </p>
              <div className="mt-1 flex flex-wrap gap-1">
                {Object.entries(p.stats).map(([k, v]) => (
                  <span key={k} className="rounded bg-[#222] px-1.5 py-0.5 text-xs">
                    {k} {v}
                  </span>
                ))}
              </div>
            </button>
          ))}
        </div>
      ) : null}

      <div>
        <label className="mb-1 block text-xs text-[#9ca3af]">Character name *</label>
        <input
          type="text"
          required
          maxLength={255}
          placeholder="Enter name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="w-full rounded border border-[#333] bg-[#101216] px-3 py-2 text-sm text-[#e6e6e6] focus:outline-none focus:ring-1 focus:ring-[#3b82f6]"
        />
      </div>
      <div className="flex gap-3">
        <div className="flex-1">
          <label className="mb-1 block text-xs text-[#9ca3af]">Class</label>
          <input
            type="text"
            required
            maxLength={255}
            value={charClass}
            onChange={(e) => setCharClass(e.target.value)}
            className="w-full rounded border border-[#333] bg-[#101216] px-3 py-2 text-sm text-[#e6e6e6] focus:outline-none focus:ring-1 focus:ring-[#3b82f6]"
          />
        </div>
        <div className="w-24">
          <label className="mb-1 block text-xs text-[#9ca3af]">HP (max)</label>
          <input
            type="number"
            min={1}
            value={hpMax}
            onChange={(e) => setHpMax(parseInt(e.target.value, 10) || 1)}
            className="w-full rounded border border-[#333] bg-[#101216] px-3 py-2 text-sm text-[#e6e6e6] focus:outline-none focus:ring-1 focus:ring-[#3b82f6]"
          />
        </div>
      </div>
      {error ? <p className="text-xs text-red-400">{error}</p> : null}
      <button
        type="submit"
        disabled={busy}
        className="rounded-md bg-[#3b82f6] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
      >
        {busy ? "Creating…" : "Create character"}
      </button>
    </form>
  );
}

export default function Characters() {
  const [chars, setChars] = useState<CharacterListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setChars(await listCharacters());
    } catch (e) {
      setError(errorMessage(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleDelete(c: CharacterListItem) {
    if (!window.confirm(`Delete character "${c.name}"?`)) return;
    setError(null);
    try {
      await deleteCharacter(c.id);
      await load();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <div className="mx-auto w-full max-w-3xl space-y-6 p-4 sm:p-6">
      <div>
        <h1 className="text-xl font-semibold">Characters</h1>
        <p className="text-sm text-[#9ca3af]">
          Your player characters. A character can be engaged in one campaign at a time.
        </p>
      </div>

      <CreateCharacterForm onCreated={() => void load()} />

      {error ? <p className="text-sm text-red-400">{error}</p> : null}

      <section className="space-y-3">
        <h2 className="text-base font-semibold">Your characters</h2>
        {chars === null ? (
          <p className="text-sm text-[#9ca3af]">Loading…</p>
        ) : chars.length === 0 ? (
          <div className="rounded-lg border border-dashed border-[#2a2c30] bg-[#15161a] p-6 text-center">
            <p className="text-sm font-medium text-[#e6e6e6]">No characters yet</p>
            <p className="mt-1 text-sm text-[#9ca3af]">
              Pick a preset above and give them a name. You'll choose a character when you start a
              session — or you can start one straight from a campaign and create one along the way.
            </p>
          </div>
        ) : (
          <ul className="space-y-2">
            {chars.map((c) => (
              <li
                key={c.id}
                className="flex items-center justify-between gap-3 rounded-lg border border-[#2a2c30] bg-[#1a1c20] px-4 py-3"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">{c.name}</p>
                  <p className="text-xs text-[#9ca3af]">
                    L{c.level} {c.character_class} · HP {c.hp_max}
                  </p>
                  <div className="mt-1.5">
                    {c.current_assignment ? (
                      <Link
                        to={`/campaigns/${c.current_assignment.campaign_id}`}
                        className="inline-block"
                      >
                        <Pill className="bg-[#1f3a5f] text-[#93c5fd] hover:underline">
                          In: {c.current_assignment.campaign_title}
                        </Pill>
                      </Link>
                    ) : (
                      <Pill className="bg-[#10331f] text-green-400">Available</Pill>
                    )}
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => void handleDelete(c)}
                  className="shrink-0 rounded border border-red-800 px-2 py-1 text-xs text-red-400 hover:bg-red-900"
                >
                  Delete
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
