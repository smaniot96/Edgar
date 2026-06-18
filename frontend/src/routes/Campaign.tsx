/**
 * Campaign view — Phase 6
 *
 * Tabs: Sessions | Characters | NPCs | World State
 */

import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import type {
  CampaignCharacterRead,
  CampaignRead,
  CharacterListItem,
  NPCRead,
  SessionRead,
  WorldFlagRead,
} from "../api/types";
import {
  ApiError,
  apiFetch,
  assignCharacterToCampaign,
  createSession,
  errorMessage,
  listCharacters,
  listSessions,
} from "../api/client";

type TabId = "sessions" | "characters" | "npcs" | "flags";

function ErrorMsg({ msg }: { msg: string }) {
  return <p className="text-sm text-red-400">{msg}</p>;
}

// ── Sessions tab ──────────────────────────────────────────────────────────────

function SessionsTab({ campaignId }: { campaignId: number }) {
  const navigate = useNavigate();
  const [sessions, setSessions] = useState<SessionRead[] | null>(null);
  const [characters, setCharacters] = useState<CharacterListItem[] | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loadCharacters = useCallback(() => {
    listCharacters()
      .then(setCharacters)
      .catch((e) => setError(errorMessage(e)));
  }, []);

  const load = useCallback(() => {
    listSessions(campaignId)
      .then((rows) => {
        const sorted = [...rows].sort(
          (a, b) => new Date(b.started_at).getTime() - new Date(a.started_at).getTime(),
        );
        setSessions(sorted);
        // First time here with no sessions? Open the picker so the path to Play is obvious.
        if (sorted.length === 0) {
          setPickerOpen(true);
          if (characters === null) loadCharacters();
        }
      })
      .catch((e) => setError(errorMessage(e)));
  }, [campaignId, characters, loadCharacters]);

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [campaignId]);

  function openPicker() {
    setError(null);
    setPickerOpen(true);
    if (characters === null) loadCharacters();
  }

  async function startSession(characterId?: number) {
    setBusy(true);
    setError(null);
    try {
      // If a PG is chosen and not yet engaged in this campaign, assign it first.
      if (characterId != null) {
        const c = characters?.find((x) => x.id === characterId);
        const alreadyHere = c?.current_assignment?.campaign_id === campaignId;
        if (!alreadyHere) {
          await assignCharacterToCampaign(campaignId, characterId);
        }
      }
      const s = await createSession(campaignId, characterId);
      localStorage.setItem("lastSessionId", String(s.id));
      navigate(`/play/${s.id}`);
    } catch (e) {
      // 409 → PG active in another campaign; surface the message.
      setError(errorMessage(e));
      setBusy(false);
    }
  }

  async function deleteSession(id: number) {
    if (!window.confirm(`Delete session #${id}?`)) return;
    try {
      await apiFetch(`/api/sessions/${id}`, { method: "DELETE" });
      load();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="max-w-xl text-xs text-[#9ca3af]">
          A session is one saved playthrough. A campaign can have several — pick up where you left
          off by hitting <strong>Continue</strong>, or start a fresh one.
        </p>
        {!pickerOpen ? (
          <button
            type="button"
            onClick={openPicker}
            className="rounded-md bg-[#3b82f6] px-3 py-1.5 text-sm font-semibold text-white"
          >
            + Start a new session
          </button>
        ) : null}
      </div>

      {pickerOpen ? (
        <div className="space-y-3 rounded-lg border border-[#2a2c30] bg-[#1a1c20] p-4">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold">Start a new session</h3>
            <button
              type="button"
              onClick={() => setPickerOpen(false)}
              className="text-xs text-[#9ca3af] hover:text-[#e6e6e6]"
            >
              Cancel
            </button>
          </div>
          <p className="text-xs text-[#9ca3af]">
            Pick the character you'll play (they'll be assigned to this campaign), or jump in without
            one and the DM will improvise.
          </p>
          {characters === null ? (
            <p className="text-sm text-[#9ca3af]">Loading characters…</p>
          ) : characters.length === 0 ? (
            <p className="text-sm italic text-[#555]">
              No characters yet —{" "}
              <Link to="/characters" className="text-[#60a5fa] hover:underline">
                create one
              </Link>
              .
            </p>
          ) : (
            <div className="grid gap-2 sm:grid-cols-2">
              {characters.map((c) => {
                const elsewhere =
                  c.current_assignment != null && c.current_assignment.campaign_id !== campaignId;
                return (
                  <button
                    key={c.id}
                    type="button"
                    disabled={busy || elsewhere}
                    onClick={() => void startSession(c.id)}
                    className="rounded-lg border border-[#2a2c30] bg-[#101216] p-3 text-left transition-colors hover:border-[#3b82f6] disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    <p className="text-sm font-medium">{c.name}</p>
                    <p className="text-xs text-[#9ca3af]">
                      L{c.level} {c.character_class} · HP {c.hp_max}
                    </p>
                    {elsewhere ? (
                      <p className="mt-1 text-xs text-yellow-400">
                        Engaged in {c.current_assignment?.campaign_title}
                      </p>
                    ) : null}
                  </button>
                );
              })}
            </div>
          )}
          <button
            type="button"
            disabled={busy}
            onClick={() => void startSession()}
            className="rounded-md border border-[#333] px-3 py-1.5 text-xs text-[#9ca3af] hover:text-[#e6e6e6] disabled:opacity-50"
          >
            {busy ? "Starting…" : "Start without a character"}
          </button>
        </div>
      ) : null}

      {error ? <ErrorMsg msg={error} /> : null}

      <div className="space-y-2">
        <h3 className="text-sm font-semibold">Existing sessions</h3>
        {sessions === null ? (
          <p className="text-sm text-[#9ca3af]">Loading…</p>
        ) : sessions.length === 0 ? (
          <p className="text-sm italic text-[#555]">
            No sessions yet — choose a character above and hit start to begin playing.
          </p>
        ) : (
          <ul className="space-y-2">
            {sessions.map((s) => (
              <li
                key={s.id}
                className="flex items-center justify-between rounded-lg border border-[#2a2c30] bg-[#1a1c20] px-4 py-3"
              >
                <div>
                  <p className="text-sm font-medium">Session #{s.id}</p>
                  <p className="text-xs text-[#9ca3af]">
                    {new Date(s.started_at).toLocaleString()}
                    {s.active_character_id ? ` · char #${s.active_character_id}` : ""}
                    {s.ended_at ? " · ended" : ""}
                  </p>
                </div>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => {
                      localStorage.setItem("lastSessionId", String(s.id));
                      navigate(`/play/${s.id}`);
                    }}
                    className="rounded bg-[#3b82f6] px-3 py-1.5 text-xs font-semibold text-white"
                  >
                    ▶ Continue
                  </button>
                  <button
                    type="button"
                    onClick={() => void deleteSession(s.id)}
                    className="rounded bg-red-800 px-2 py-1 text-xs text-white hover:bg-red-700"
                  >
                    Delete
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

// ── Characters tab ────────────────────────────────────────────────────────────

function CharactersTab({ campaignId }: { campaignId: number }) {
  const [chars, setChars] = useState<CampaignCharacterRead[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiFetch(`/api/campaigns/${campaignId}/characters`)
      .then((r) => r.json() as Promise<CampaignCharacterRead[]>)
      .then(setChars)
      .catch((e) => setError(e instanceof ApiError ? e.body : String(e)));
  }, [campaignId]);

  return (
    <div className="space-y-4">
      <p className="text-xs text-[#9ca3af]">
        Characters currently engaged in this campaign. Assign a character when starting a session, or
        manage your roster in{" "}
        <Link to="/characters" className="text-[#60a5fa] hover:underline">
          Characters
        </Link>
        .
      </p>
      {error ? <ErrorMsg msg={error} /> : null}
      {chars === null ? (
        <p className="text-sm text-[#9ca3af]">Loading…</p>
      ) : chars.length === 0 ? (
        <p className="text-sm italic text-[#555]">No characters assigned yet.</p>
      ) : (
        <ul className="space-y-2">
          {chars.map((c) => (
            <li
              key={c.assignment_id}
              className="flex items-center justify-between rounded-lg border border-[#2a2c30] bg-[#1a1c20] px-4 py-3"
            >
              <div>
                <p className="text-sm font-medium">{c.name}</p>
                <p className="text-xs text-[#9ca3af]">
                  L{c.level} {c.character_class} · HP {c.hp_current}/{c.hp_max}
                </p>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ── NPCs tab ──────────────────────────────────────────────────────────────────

function NpcsTab({ campaignId }: { campaignId: number }) {
  const [npcs, setNpcs] = useState<NPCRead[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [npcName, setNpcName] = useState("");
  const [disposition, setDisposition] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    apiFetch(`/api/campaigns/${campaignId}/npcs`)
      .then((r) => r.json() as Promise<NPCRead[]>)
      .then(setNpcs)
      .catch((e) => setError(e instanceof ApiError ? e.body : String(e)));
  }, [campaignId]);

  useEffect(() => { load(); }, [load]);

  async function addNpc(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await apiFetch(`/api/campaigns/${campaignId}/npcs`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ name: npcName.trim(), disposition: disposition.trim(), stat_block: {} }),
      });
      setNpcName("");
      setDisposition("");
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.body : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function deleteNpc(id: number, name: string) {
    if (!window.confirm(`Delete ${name}?`)) return;
    try {
      await apiFetch(`/api/npcs/${id}`, { method: "DELETE" });
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.body : String(e));
    }
  }

  return (
    <div className="space-y-4">
      <form
        onSubmit={(e) => void addNpc(e)}
        className="space-y-3 rounded-lg border border-[#2a2c30] bg-[#1a1c20] p-4"
      >
        <h3 className="text-sm font-semibold">Add NPC</h3>
        <div className="flex gap-3">
          <input
            type="text"
            required
            maxLength={255}
            placeholder="Name"
            value={npcName}
            onChange={(e) => setNpcName(e.target.value)}
            className="flex-1 rounded border border-[#333] bg-[#101216] px-3 py-1.5 text-sm text-[#e6e6e6] focus:outline-none focus:ring-1 focus:ring-[#3b82f6]"
          />
          <input
            type="text"
            required
            maxLength={255}
            placeholder="Disposition (friendly, hostile…)"
            value={disposition}
            onChange={(e) => setDisposition(e.target.value)}
            className="flex-1 rounded border border-[#333] bg-[#101216] px-3 py-1.5 text-sm text-[#e6e6e6] focus:outline-none focus:ring-1 focus:ring-[#3b82f6]"
          />
          <button
            type="submit"
            disabled={busy}
            className="rounded bg-[#3b82f6] px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-50"
          >
            Add
          </button>
        </div>
      </form>
      {error ? <ErrorMsg msg={error} /> : null}
      {npcs === null ? (
        <p className="text-sm text-[#9ca3af]">Loading…</p>
      ) : npcs.length === 0 ? (
        <p className="text-sm italic text-[#555]">No NPCs yet.</p>
      ) : (
        <ul className="space-y-2">
          {npcs.map((n) => (
            <li
              key={n.id}
              className="flex items-center justify-between rounded-lg border border-[#2a2c30] bg-[#1a1c20] px-4 py-3"
            >
              <div>
                <p className="text-sm font-medium">{n.name}</p>
                <span className="inline-block rounded-full bg-[#222] px-2 py-0.5 text-xs">
                  {n.disposition}
                </span>
              </div>
              <button
                type="button"
                onClick={() => void deleteNpc(n.id, n.name)}
                className="rounded bg-red-800 px-2 py-1 text-xs text-white hover:bg-red-700"
              >
                Delete
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ── World State tab ───────────────────────────────────────────────────────────

function WorldStateTab({ campaignId }: { campaignId: number }) {
  const [flags, setFlags] = useState<WorldFlagRead[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fKey, setFKey] = useState("");
  const [fVal, setFVal] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    apiFetch(`/api/campaigns/${campaignId}/world-flags`)
      .then((r) => r.json() as Promise<WorldFlagRead[]>)
      .then(setFlags)
      .catch((e) => setError(e instanceof ApiError ? e.body : String(e)));
  }, [campaignId]);

  useEffect(() => { load(); }, [load]);

  async function setFlag(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await apiFetch(`/api/campaigns/${campaignId}/world-flags`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ key: fKey.trim(), value: fVal.trim() }),
      });
      setFKey("");
      setFVal("");
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.body : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function saveFlag(key: string, value: string) {
    try {
      await apiFetch(`/api/campaigns/${campaignId}/world-flags/${key}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ value }),
      });
    } catch (e) {
      setError(e instanceof ApiError ? e.body : String(e));
    }
  }

  async function deleteFlag(key: string) {
    if (!window.confirm(`Delete flag "${key}"?`)) return;
    try {
      await apiFetch(`/api/campaigns/${campaignId}/world-flags/${key}`, { method: "DELETE" });
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.body : String(e));
    }
  }

  return (
    <div className="space-y-4">
      <p className="rounded bg-[#3b2900] px-3 py-2 text-xs text-yellow-400">
        These flags are normally set by the adjudicator; manual edits override game state.
      </p>
      <form
        onSubmit={(e) => void setFlag(e)}
        className="space-y-3 rounded-lg border border-[#2a2c30] bg-[#1a1c20] p-4"
      >
        <h3 className="text-sm font-semibold">Set flag</h3>
        <div className="flex gap-3">
          <input
            type="text"
            required
            maxLength={255}
            placeholder="key"
            value={fKey}
            onChange={(e) => setFKey(e.target.value)}
            className="flex-1 rounded border border-[#333] bg-[#101216] px-3 py-1.5 text-sm text-[#e6e6e6] focus:outline-none focus:ring-1 focus:ring-[#3b82f6]"
          />
          <input
            type="text"
            required
            maxLength={255}
            placeholder="value"
            value={fVal}
            onChange={(e) => setFVal(e.target.value)}
            className="flex-1 rounded border border-[#333] bg-[#101216] px-3 py-1.5 text-sm text-[#e6e6e6] focus:outline-none focus:ring-1 focus:ring-[#3b82f6]"
          />
          <button
            type="submit"
            disabled={busy}
            className="rounded bg-[#3b82f6] px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-50"
          >
            Set
          </button>
        </div>
      </form>
      {error ? <ErrorMsg msg={error} /> : null}
      {flags === null ? (
        <p className="text-sm text-[#9ca3af]">Loading…</p>
      ) : flags.length === 0 ? (
        <p className="text-sm italic text-[#555]">No world flags set.</p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-[#9ca3af]">
              <th className="pb-2 pr-4">Key</th>
              <th className="pb-2 pr-4">Value</th>
              <th className="pb-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-[#222]">
            {flags.map((f) => (
              <FlagRow
                key={f.key}
                flag={f}
                onSave={(v) => void saveFlag(f.key, v)}
                onDelete={() => void deleteFlag(f.key)}
              />
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function FlagRow({
  flag,
  onSave,
  onDelete,
}: {
  flag: WorldFlagRead;
  onSave: (value: string) => void;
  onDelete: () => void;
}) {
  const [val, setVal] = useState(flag.value);

  return (
    <tr>
      <td className="py-1.5 pr-4 font-mono text-xs">{flag.key}</td>
      <td className="py-1.5 pr-2">
        <input
          type="text"
          maxLength={255}
          value={val}
          onChange={(e) => setVal(e.target.value)}
          className="w-full rounded border border-[#333] bg-[#101216] px-2 py-1 text-xs text-[#e6e6e6] focus:outline-none focus:ring-1 focus:ring-[#3b82f6]"
        />
      </td>
      <td className="py-1.5">
        <div className="flex gap-1">
          <button
            type="button"
            onClick={() => onSave(val)}
            className="rounded px-2 py-0.5 text-xs text-[#60a5fa] hover:underline"
          >
            Save
          </button>
          <button
            type="button"
            onClick={onDelete}
            className="rounded px-2 py-0.5 text-xs text-red-400 hover:underline"
          >
            Del
          </button>
        </div>
      </td>
    </tr>
  );
}

// ── Main campaign view ────────────────────────────────────────────────────────

const TABS: { id: TabId; label: string }[] = [
  { id: "sessions", label: "Sessions" },
  { id: "characters", label: "Characters" },
  { id: "npcs", label: "NPCs" },
  { id: "flags", label: "World State" },
];

export default function CampaignView() {
  const { id } = useParams<{ id: string }>();
  const campaignId = Number(id);
  const navigate = useNavigate();
  const [tab, setTab] = useState<TabId>("sessions");
  const [campaign, setCampaign] = useState<CampaignRead | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!campaignId) return;
    apiFetch(`/api/campaigns/${campaignId}`)
      .then((r) => r.json() as Promise<CampaignRead>)
      .then(setCampaign)
      .catch((e) => setLoadError(e instanceof ApiError ? e.body : String(e)));
  }, [campaignId]);

  async function deleteCampaign() {
    if (!window.confirm(`Delete campaign "${campaign?.title}"? This cannot be undone.`)) return;
    try {
      await apiFetch(`/api/campaigns/${campaignId}`, { method: "DELETE" });
      navigate("/campaigns");
    } catch (e) {
      setLoadError(e instanceof ApiError ? e.body : String(e));
    }
  }

  if (loadError) {
    return (
      <div className="p-6">
        <Link to="/campaigns" className="text-sm text-[#60a5fa] hover:underline">← Campaigns</Link>
        <ErrorMsg msg={loadError} />
      </div>
    );
  }

  if (!campaign) {
    return (
      <div className="p-6 text-sm text-[#9ca3af]">Loading campaign…</div>
    );
  }

  return (
    <div className="flex flex-1 flex-col">
      <div className="border-b border-[#222] px-4 py-3">
        <div className="flex items-center justify-between">
          <div>
            <Link to="/campaigns" className="text-xs text-[#60a5fa] hover:underline">← Campaigns</Link>
            <h1 className="mt-0.5 text-lg font-semibold">{campaign.title}</h1>
            <p className="text-xs text-[#9ca3af]">
              {campaign.adventure_collections.join(", ") || "no adventure"} · {campaign.system}
            </p>
          </div>
          <button
            type="button"
            onClick={() => void deleteCampaign()}
            className="rounded border border-red-800 px-2 py-1 text-xs text-red-400 hover:bg-red-900"
          >
            Delete campaign
          </button>
        </div>
        <div className="mt-3 flex gap-0">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => setTab(t.id)}
              className={`border-b-2 px-4 py-2 text-sm transition-colors ${
                tab === t.id
                  ? "border-[#3b82f6] text-[#e6e6e6]"
                  : "border-transparent text-[#9ca3af] hover:text-[#e6e6e6]"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>
      <div className="p-4 sm:p-6">
        {tab === "sessions" && <SessionsTab campaignId={campaignId} />}
        {tab === "characters" && <CharactersTab campaignId={campaignId} />}
        {tab === "npcs" && <NpcsTab campaignId={campaignId} />}
        {tab === "flags" && <WorldStateTab campaignId={campaignId} />}
      </div>
    </div>
  );
}
