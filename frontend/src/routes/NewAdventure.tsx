/**
 * New-adventure wizard — Phase 5
 *
 * Step 1: Pick adventure module
 * Step 2: Name campaign
 * Step 3: Build character (from preset)
 */

import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import type { AdventureRead, CampaignRead, CharacterPreset, CharacterRead } from "../api/types";
import { ApiError, apiFetch } from "../api/client";

type Step = 1 | 2 | 3;

// ── Step 1: Adventure picker ──────────────────────────────────────────────────

function Step1({
  onNext,
}: {
  onNext: (adv: AdventureRead) => void;
}) {
  const [adventures, setAdventures] = useState<AdventureRead[] | null>(null);
  const [selected, setSelected] = useState<AdventureRead | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiFetch("/api/adventures")
      .then((r) => r.json() as Promise<AdventureRead[]>)
      .then(setAdventures)
      .catch((e) => setError(e instanceof ApiError ? e.body : String(e)));
  }, []);

  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold">Step 1 of 3 — Pick an adventure</h2>
      {error ? <p className="text-sm text-red-400">{error}</p> : null}
      {adventures === null ? (
        <p className="text-sm text-[#9ca3af]">Loading adventures…</p>
      ) : adventures.length === 0 ? (
        <p className="text-sm italic text-[#555]">
          No adventure modules installed. Ask a developer to run <code>make ingest</code>.
        </p>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {adventures.map((adv) => (
            <button
              key={adv.slug}
              type="button"
              onClick={() => setSelected(adv)}
              className={`rounded-lg border p-4 text-left transition-colors ${
                selected?.slug === adv.slug
                  ? "border-[#3b82f6] bg-[#1f3a5f]"
                  : "border-[#2a2c30] bg-[#1a1c20] hover:border-[#3b82f6]"
              }`}
            >
              {adv.cover_image ? (
                <img
                  src={`/${adv.cover_image}`}
                  alt={adv.title}
                  className="mb-2 w-full rounded object-cover"
                  style={{ maxHeight: 120 }}
                />
              ) : null}
              <p className="text-sm font-semibold">{adv.title}</p>
              {adv.description ? (
                <p className="mt-1 text-xs text-[#9ca3af] line-clamp-2">{adv.description}</p>
              ) : null}
              {adv.level_range ? (
                <span className="mt-2 inline-block rounded-full bg-[#222] px-2 py-0.5 text-xs">
                  Levels {adv.level_range}
                </span>
              ) : null}
            </button>
          ))}
        </div>
      )}
      <button
        type="button"
        disabled={!selected}
        onClick={() => selected && onNext(selected)}
        className="rounded-md bg-[#3b82f6] px-4 py-2 text-sm font-semibold text-white disabled:opacity-40"
      >
        Next →
      </button>
    </div>
  );
}

// ── Step 2: Campaign name ──────────────────────────────────────────────────────

function Step2({
  adventure,
  onBack,
  onNext,
}: {
  adventure: AdventureRead;
  onBack: () => void;
  onNext: (campaign: CampaignRead) => void;
}) {
  const [title, setTitle] = useState(adventure.title);
  const [system, setSystem] = useState("D&D 5e");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await apiFetch("/api/campaigns", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          title,
          system,
          adventure_collections: [adventure.slug],
        }),
      });
      const camp = (await res.json()) as CampaignRead;
      onNext(camp);
    } catch (err) {
      setError(err instanceof ApiError ? err.body : String(err));
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold">Step 2 of 3 — Name your campaign</h2>
      <form onSubmit={(e) => void submit(e)} className="space-y-3">
        <div>
          <label className="mb-1 block text-xs text-[#9ca3af]">Campaign title</label>
          <input
            type="text"
            required
            maxLength={255}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className="w-full rounded border border-[#333] bg-[#101216] px-3 py-2 text-sm text-[#e6e6e6] focus:outline-none focus:ring-1 focus:ring-[#3b82f6]"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-[#9ca3af]">System</label>
          <input
            type="text"
            required
            maxLength={255}
            value={system}
            onChange={(e) => setSystem(e.target.value)}
            className="w-full rounded border border-[#333] bg-[#101216] px-3 py-2 text-sm text-[#e6e6e6] focus:outline-none focus:ring-1 focus:ring-[#3b82f6]"
          />
        </div>
        {error ? <p className="text-xs text-red-400">{error}</p> : null}
        <div className="flex gap-2">
          <button
            type="button"
            onClick={onBack}
            className="rounded px-3 py-2 text-sm text-[#60a5fa] hover:underline"
          >
            ← Back
          </button>
          <button
            type="submit"
            disabled={busy}
            className="rounded-md bg-[#3b82f6] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
          >
            {busy ? "Creating…" : "Next →"}
          </button>
        </div>
      </form>
    </div>
  );
}

// ── Step 3: Character builder ─────────────────────────────────────────────────

function Step3({
  campaign,
  onBack,
  onFinish,
}: {
  campaign: CampaignRead;
  onBack: () => void;
  onFinish: (sessionId: number) => void;
}) {
  const [presets, setPresets] = useState<CharacterPreset[] | null>(null);
  const [selected, setSelected] = useState<CharacterPreset | null>(null);
  const [name, setName] = useState("");
  const [charClass, setCharClass] = useState("");
  const [hpMax, setHpMax] = useState(10);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiFetch("/api/character-presets")
      .then((r) => r.json() as Promise<CharacterPreset[]>)
      .then((ps) => {
        setPresets(ps);
        if (ps.length > 0) applyPreset(ps[0]);
      })
      .catch((e) => setError(e instanceof ApiError ? e.body : String(e)));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  function applyPreset(p: CharacterPreset) {
    setSelected(p);
    setCharClass(p.character_class);
    setHpMax(p.hp_max);
  }

  async function finish(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const charRes = await apiFetch("/api/characters", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          name: name.trim(),
          character_class: charClass,
          level: selected?.level ?? 1,
          hp_max: hpMax,
          base_stats: selected?.stats ?? {},
          base_inventory: selected?.inventory ?? {},
        }),
      });
      const character = (await charRes.json()) as CharacterRead;

      const sessRes = await apiFetch("/api/sessions", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ campaign_id: campaign.id }),
      });
      const session = (await sessRes.json()) as { id: number };

      await apiFetch(`/api/sessions/${session.id}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ active_character_id: character.id }),
      });

      localStorage.setItem("lastSessionId", String(session.id));
      onFinish(session.id);
    } catch (err) {
      setError(err instanceof ApiError ? err.body : String(err));
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold">Step 3 of 3 — Build your character</h2>
      {presets === null ? (
        <p className="text-sm text-[#9ca3af]">Loading presets…</p>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {presets.map((p) => (
            <button
              key={p.key}
              type="button"
              onClick={() => applyPreset(p)}
              className={`rounded-lg border p-3 text-left text-sm transition-colors ${
                selected?.key === p.key
                  ? "border-[#3b82f6] bg-[#1f3a5f]"
                  : "border-[#2a2c30] bg-[#1a1c20] hover:border-[#3b82f6]"
              }`}
            >
              <p className="font-medium">{p.label}</p>
              <p className="mt-1 text-xs text-[#9ca3af]">HP {p.hp_max}</p>
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
      )}

      <form onSubmit={(e) => void finish(e)} className="space-y-3 pt-2">
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
              onChange={(e) => setHpMax(parseInt(e.target.value, 10))}
              className="w-full rounded border border-[#333] bg-[#101216] px-3 py-2 text-sm text-[#e6e6e6] focus:outline-none focus:ring-1 focus:ring-[#3b82f6]"
            />
          </div>
        </div>
        {error ? <p className="text-xs text-red-400">{error}</p> : null}
        <div className="flex gap-2">
          <button
            type="button"
            onClick={onBack}
            className="rounded px-3 py-2 text-sm text-[#60a5fa] hover:underline"
          >
            ← Back
          </button>
          <button
            type="submit"
            disabled={busy}
            className="rounded-md bg-[#3b82f6] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
          >
            {busy ? "Starting…" : "Finish & Play"}
          </button>
        </div>
      </form>
    </div>
  );
}

// ── Wizard shell ──────────────────────────────────────────────────────────────

export default function NewAdventure() {
  const navigate = useNavigate();
  const [step, setStep] = useState<Step>(1);
  const [adventure, setAdventure] = useState<AdventureRead | null>(null);
  const [campaign, setCampaign] = useState<CampaignRead | null>(null);

  return (
    <div className="mx-auto max-w-2xl p-6">
      <h1 className="mb-6 text-xl font-semibold">New adventure</h1>
      {step === 1 && (
        <Step1
          onNext={(adv) => {
            setAdventure(adv);
            setStep(2);
          }}
        />
      )}
      {step === 2 && adventure && (
        <Step2
          adventure={adventure}
          onBack={() => setStep(1)}
          onNext={(camp) => {
            setCampaign(camp);
            setStep(3);
          }}
        />
      )}
      {step === 3 && campaign && (
        <Step3
          campaign={campaign}
          onBack={() => setStep(2)}
          onFinish={(sessionId) => navigate(`/play/${sessionId}`)}
        />
      )}
    </div>
  );
}
