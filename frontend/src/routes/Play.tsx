import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import type {
  AdjudicationResult,
  CampaignRead,
  CharacterAssignmentHistoryRead,
  CharacterRead,
  ChatMessageRead,
  CombatState,
  DebugPayload,
  SessionRead,
  TurnResult,
} from "../api/types";
import { ComposerBar } from "../components/ComposerBar";
import { MessageList, type Line } from "../components/MessageList";
import { Pill } from "../components/Pill";
import { useMode } from "../dev/ModeContext";
import { DevBadge, ModeToggle } from "../dev/ModeToggle";
import { ApiError, apiBase, apiFetch } from "../hooks/useApi";
import { useSseTurnStream } from "../hooks/useSSE";
import { cn } from "../lib/cn";

const STATUS_STAGES = ["parsing", "retrieving", "adjudicating", "narrating", "saving"] as const;

const STATUS_LABELS: Record<string, string> = {
  parsing: "Parsing…",
  retrieving: "Searching rules…",
  adjudicating: "Adjudicating…",
  narrating: "Narrating…",
  saving: "Saving…",
};

function formatStatus(stage: string): string {
  return STATUS_LABELS[stage] ?? stage;
}

interface Hp {
  current: number | null;
  max: number | null;
}

function hpRatio(hp: Hp): number | null {
  if (hp.current == null || hp.max == null || hp.max <= 0) return null;
  return hp.current / hp.max;
}

function HpPill({ hp }: { hp: Hp }) {
  const ratio = hpRatio(hp);
  const text = hp.current == null || hp.max == null ? "HP —/—" : `HP ${hp.current}/${hp.max}`;
  const danger = ratio != null && ratio <= 0;
  const low = ratio != null && ratio > 0 && ratio <= 0.25;
  return (
    <Pill
      className={cn(
        danger && "bg-red-900/70 font-semibold text-red-200",
        low && "bg-amber-900/60 font-semibold text-amber-200",
      )}
    >
      {danger ? `${text} · Defeated` : text}
    </Pill>
  );
}

function CombatHud({ combat }: { combat: CombatState }) {
  return (
    <div className="border-b border-[#222] bg-[#15161a] px-4 py-2">
      <div className="mb-1 flex items-center gap-2 text-xs uppercase tracking-wide text-[#9a9a9a]">
        <span>Combat</span>
        <span className="text-[#cfcfcf]">Round {combat.round}</span>
      </div>
      <div className="flex flex-wrap gap-2">
        {combat.combatants
          .filter((c) => !c.is_player)
          .map((c) => {
            const ratio = c.hp_max > 0 ? c.hp_current / c.hp_max : null;
            const low = ratio != null && ratio <= 0.25;
            return (
              <span
                key={c.name}
                className={cn(
                  "rounded-md border border-[#333] bg-[#1a1c20] px-2 py-0.5 text-xs",
                  !c.alive && "opacity-50 line-through",
                  c.alive && low && "border-amber-700 text-amber-200",
                )}
              >
                {c.name} {c.hp_current}/{c.hp_max}
              </span>
            );
          })}
      </div>
    </div>
  );
}

export default function Play() {
  const { sessionId: sessionIdParam } = useParams<{ sessionId: string }>();
  const sessionId = Number(sessionIdParam);
  const { consume } = useSseTurnStream();
  const { mode, isDev } = useMode();

  const [title, setTitle] = useState("Edgar");
  const [charPill, setCharPill] = useState("—");
  const [hp, setHp] = useState<Hp>({ current: null, max: null });
  const [scenePill, setScenePill] = useState("scene —");
  const [turnStatus, setTurnStatus] = useState<string | null>(null);
  const [currentStage, setCurrentStage] = useState<string | null>(null);
  const [combat, setCombat] = useState<CombatState | null>(null);
  const [campaignComplete, setCampaignComplete] = useState(false);

  const [lines, setLines] = useState<Line[]>([]);
  const [streamingDm, setStreamingDm] = useState("");
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [bootError, setBootError] = useState<string | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(true);

  const refreshHeader = useCallback(async (sid: number) => {
    const sRes = await apiFetch(`/api/sessions/${sid}`);
    const s = (await sRes.json()) as SessionRead;
    setScenePill(`scene ${s.current_scene_id ?? "—"}`);
    try {
      const cRes = await apiFetch(`/api/campaigns/${s.campaign_id}`);
      const camp = (await cRes.json()) as CampaignRead;
      setTitle(camp.title || "Edgar");
      if (camp.status === "ended") setCampaignComplete(true);
    } catch {
      setTitle("Edgar");
    }
    if (s.active_character_id) {
      const cRes = await apiFetch(`/api/characters/${s.active_character_id}`);
      const c = (await cRes.json()) as CharacterRead;
      let hpCur = c.hp_max;
      let hpMx = c.hp_max;
      try {
        const assRes = await apiFetch(`/api/characters/${s.active_character_id}/assignments`);
        const assigns = (await assRes.json()) as CharacterAssignmentHistoryRead[];
        const active = assigns.find((a) => a.status === "active");
        if (active) {
          hpCur = active.hp_current;
          hpMx = active.hp_max;
        }
      } catch {
        /* assignment history optional for header */
      }
      setCharPill(`${c.name} (L${c.level} ${c.character_class})`);
      setHp({ current: hpCur, max: hpMx });
    } else {
      setCharPill("—");
      setHp({ current: null, max: null });
    }
  }, []);

  const loadHistory = useCallback(
    async (sid: number): Promise<number> => {
      setLoadingHistory(true);
      const r = await apiFetch(`/api/sessions/${sid}/messages`);
      const msgs = (await r.json()) as ChatMessageRead[];
      setLines(
        msgs.map((m) => ({
          role: m.role,
          content: m.content,
          // Past DM turns from history have no captured debug; the dev panel will
          // show "no debug captured" so dev mode is still informative.
          isTurn: m.role === "dm",
        })),
      );
      setLoadingHistory(false);
      return msgs.length;
    },
    [],
  );

  // Generate the opening scene the first time an empty session is opened, so the player lands
  // on a DM-set scene instead of a blank chat.
  const ensureIntro = useCallback(async (sid: number) => {
    setTurnStatus("Setting the scene…");
    try {
      const r = await apiFetch(`/api/sessions/${sid}/intro`, { method: "POST" });
      const data = (await r.json()) as {
        started: boolean;
        narration?: string;
        current_scene_id?: string | null;
      };
      if (data.started && data.narration) {
        setLines((prev) => (prev.length === 0 ? [{ role: "dm", content: data.narration as string }] : prev));
        if (data.current_scene_id !== undefined) {
          setScenePill(`scene ${data.current_scene_id ?? "—"}`);
        }
      }
    } catch {
      /* non-fatal: the player can still type a first action */
    } finally {
      setTurnStatus(null);
    }
  }, []);

  useEffect(() => {
    if (!Number.isFinite(sessionId) || sessionId < 1) {
      setBootError("Invalid session id");
      setLoadingHistory(false);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        await refreshHeader(sessionId);
        const count = await loadHistory(sessionId);
        if (!cancelled && count === 0) {
          await ensureIntro(sessionId);
        }
      } catch (e) {
        if (!cancelled) {
          const msg = e instanceof ApiError ? e.body : String(e);
          setBootError(msg);
          setLoadingHistory(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sessionId, refreshHeader, loadHistory, ensureIntro]);

  // Apply the post-turn payload (done frame or sync turn response) directly,
  // avoiding redundant refresh fetches and stale state from overlapping turns.
  const applyTurnResult = useCallback((data: TurnResult) => {
    if (data.character) {
      const c = data.character;
      if (typeof c.hp_current === "number" || typeof c.hp_max === "number") {
        setHp((prev) => ({
          current: typeof c.hp_current === "number" ? c.hp_current : prev.current,
          max: typeof c.hp_max === "number" ? c.hp_max : prev.max,
        }));
      }
      if (c.name && c.level != null && c.character_class) {
        setCharPill(`${c.name} (L${c.level} ${c.character_class})`);
      }
    }
    if (data.current_scene_id !== undefined) {
      setScenePill(`scene ${data.current_scene_id ?? "—"}`);
    }
    // Hide the HUD when combat ends; keep it while combat is ongoing.
    setCombat(data.combat_state && !data.combat_state.ended ? data.combat_state : null);
    if (data.campaign_complete) setCampaignComplete(true);
  }, []);

  async function send() {
    const msg = draft.trim();
    if (!msg || !Number.isFinite(sessionId) || busy || campaignComplete) return;
    setBusy(true);
    setDraft("");
    setLines((prev) => [...prev, { role: "user", content: msg }]);
    setStreamingDm("");
    setTurnStatus(formatStatus("parsing"));
    setCurrentStage("parsing");

    const debug = mode === "dev";
    let dmAccum = "";
    let pendingAdj: AdjudicationResult | null = null;
    let pendingDebug: DebugPayload | null = null;

    try {
      const r = await fetch(`${apiBase}/api/sessions/${sessionId}/turn/stream`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ message: msg, debug }),
      });
      if (!r.ok) {
        setTurnStatus(null);
        const detail = await r.text();
        if (r.status === 409) {
          if (/has ended|campaign has ended/i.test(detail)) {
            setCampaignComplete(true);
            setLines((prev) => [
              ...prev,
              { role: "dm", content: "This campaign has ended. The adventure is complete." },
            ]);
          } else {
            // Lock contention: another turn is in progress.
            setLines((prev) => [
              ...prev,
              { role: "dm", content: "A turn is already in progress — please wait a moment and retry." },
            ]);
            setDraft(msg);
          }
        } else {
          let friendly = detail;
          try {
            const j = JSON.parse(detail) as { detail?: string };
            if (j.detail) friendly = j.detail;
          } catch {
            /* keep raw text */
          }
          setLines((prev) => [...prev, { role: "dm", content: `Something went wrong (${r.status}): ${friendly}` }]);
        }
        return;
      }

      await consume(r, {
        onStatus: (stage) => {
          setTurnStatus(stage ? formatStatus(stage) : null);
          setCurrentStage(stage || null);
        },
        onToken: (text) => {
          dmAccum += text;
          setStreamingDm(dmAccum);
        },
        onAdjudication: (result) => {
          pendingAdj = result;
        },
        onDebug: (payload) => {
          pendingDebug = payload;
        },
        onDone: (data) => {
          setTurnStatus(null);
          setCurrentStage(null);
          const result = data as TurnResult;
          const narration =
            typeof result.narration === "string" && result.narration.trim()
              ? result.narration
              : "[no narration]";
          const narrationText = dmAccum.trim() ? dmAccum : narration;
          const ended = result.combat_state?.ended ? result.combat_state.outcome : undefined;
          setLines((prev) => [
            ...prev,
            {
              role: "dm",
              content: narrationText,
              adjudication: pendingAdj,
              debug: pendingDebug,
              combatOutcome: ended,
              isTurn: true,
            },
          ]);
          setStreamingDm("");
          applyTurnResult(result);
        },
        onError: (detail) => {
          setTurnStatus(null);
          setCurrentStage(null);
          // Preserve any partial narration the player was reading.
          setLines((prev) => {
            const next = [...prev];
            if (dmAccum.trim()) {
              next.push({
                role: "dm",
                content: dmAccum,
                adjudication: pendingAdj,
                debug: pendingDebug,
                isTurn: true,
              });
            }
            next.push({ role: "dm", content: `The turn could not be completed: ${detail}` });
            return next;
          });
          setStreamingDm("");
        },
      });
      setTurnStatus(null);
    } catch (e) {
      setTurnStatus(null);
      setCurrentStage(null);
      setLines((prev) => [
        ...prev,
        { role: "dm", content: `Connection problem: ${String(e)}. Please try again.` },
      ]);
    } finally {
      setBusy(false);
      setStreamingDm("");
    }
  }

  if (!Number.isFinite(sessionId) || sessionId < 1) {
    return (
      <div className="flex flex-1 items-center justify-center p-6 text-red-400">
        Invalid session id
      </div>
    );
  }

  if (bootError) {
    return (
      <div className="flex flex-1 flex-col gap-2 p-6">
        <p className="text-red-400">Could not load session.</p>
        <pre className="whitespace-pre-wrap text-sm text-[#aaa]">{bootError}</pre>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col text-[#e6e6e6]">
      <header className="flex flex-wrap items-center gap-x-4 gap-y-3 border-b border-[#222] px-4 py-3">
        <strong className="mr-auto text-[1.05rem]">{title}</strong>
        <Pill>{charPill}</Pill>
        <HpPill hp={hp} />
        <Pill>{scenePill}</Pill>
        {turnStatus ? (
          <Pill className="italic opacity-85">{turnStatus}</Pill>
        ) : null}
        {/* Mode is flippable mid-campaign without leaving the game. */}
        <DevBadge />
        <ModeToggle />
      </header>

      {/* In dev mode, surface the live pipeline stages prominently. */}
      {isDev ? (
        <div className="flex flex-wrap items-center gap-1.5 border-b border-amber-900/40 bg-amber-950/20 px-4 py-1.5 text-[0.7rem]">
          <span className="mr-1 font-semibold uppercase tracking-wide text-amber-300">Pipeline</span>
          {STATUS_STAGES.map((stage) => {
            const active = currentStage === stage;
            return (
              <span
                key={stage}
                className={cn(
                  "rounded-full border px-2 py-0.5 font-mono",
                  active
                    ? "border-amber-500 bg-amber-700/60 text-amber-50"
                    : "border-[#2a2d33] bg-[#15161a] text-[#8a8a8a]",
                )}
              >
                {stage}
              </span>
            );
          })}
        </div>
      ) : null}

      {combat ? <CombatHud combat={combat} /> : null}

      {campaignComplete ? (
        <div className="border-b border-emerald-800 bg-emerald-950/60 px-4 py-3 text-center text-emerald-200">
          <div className="text-lg font-semibold">🏆 Campaign Complete</div>
          <div className="text-sm opacity-90">The adventure has ended. Well played!</div>
        </div>
      ) : null}

      {loadingHistory ? (
        <div className="flex flex-1 items-center px-4 py-6">
          <div className="rounded-lg bg-[#1f1f23] px-3 py-2.5">Loading…</div>
        </div>
      ) : (
        <MessageList messages={lines} streamingDm={streamingDm} />
      )}

      <ComposerBar
        value={draft}
        onChange={setDraft}
        onSend={() => void send()}
        disabled={busy || campaignComplete}
      />
    </div>
  );
}
