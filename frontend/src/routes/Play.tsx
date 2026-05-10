import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import type {
  CampaignRead,
  CharacterAssignmentHistoryRead,
  CharacterRead,
  ChatMessageRead,
  SessionRead,
} from "../api/types";
import { ComposerBar } from "../components/ComposerBar";
import { MessageList, type Line } from "../components/MessageList";
import { Pill } from "../components/Pill";
import { ApiError, apiBase, apiFetch } from "../hooks/useApi";
import { useSseTurnStream } from "../hooks/useSSE";

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

export default function Play() {
  const { sessionId: sessionIdParam } = useParams<{ sessionId: string }>();
  const sessionId = Number(sessionIdParam);
  const { consume } = useSseTurnStream();

  const [title, setTitle] = useState("Edgar");
  const [charPill, setCharPill] = useState("—");
  const [hpPill, setHpPill] = useState("HP —/—");
  const [scenePill, setScenePill] = useState("scene —");
  const [turnStatus, setTurnStatus] = useState<string | null>(null);

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
      setHpPill(`HP ${hpCur}/${hpMx}`);
    } else {
      setCharPill("—");
      setHpPill("HP —/—");
    }
  }, []);

  const loadHistory = useCallback(
    async (sid: number) => {
      setLoadingHistory(true);
      const r = await apiFetch(`/api/sessions/${sid}/messages`);
      const msgs = (await r.json()) as ChatMessageRead[];
      setLines(msgs.map((m) => ({ role: m.role, content: m.content })));
      setLoadingHistory(false);
    },
    [],
  );

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
        await loadHistory(sessionId);
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
  }, [sessionId, refreshHeader, loadHistory]);

  async function send() {
    const msg = draft.trim();
    if (!msg || !Number.isFinite(sessionId)) return;
    setBusy(true);
    setDraft("");
    setLines((prev) => [...prev, { role: "user", content: msg }]);
    setStreamingDm("");
    setTurnStatus(formatStatus("parsing"));

    let dmAccum = "";

    try {
      const r = await fetch(`${apiBase}/api/sessions/${sessionId}/turn/stream`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ message: msg }),
      });
      if (!r.ok) {
        setTurnStatus(null);
        const detail = await r.text();
        setLines((prev) => [...prev, { role: "dm", content: `[error ${r.status}: ${detail}]` }]);
        await refreshHeader(sessionId);
        return;
      }

      await consume(r, {
        onStatus: (stage) => {
          setTurnStatus(stage ? formatStatus(stage) : null);
        },
        onToken: (text) => {
          dmAccum += text;
          setStreamingDm(dmAccum);
        },
        onDone: (data) => {
          setTurnStatus(null);
          const narration =
            typeof data.narration === "string" && data.narration.trim()
              ? data.narration
              : "[no narration]";
          setLines((prev) => {
            const next = [...prev];
            const narrationText = dmAccum.trim() ? dmAccum : narration;
            next.push({ role: "dm", content: narrationText });
            return next;
          });
          setStreamingDm("");
          void refreshHeader(sessionId);
        },
        onError: (detail) => {
          setTurnStatus(null);
          setLines((prev) => [
            ...prev,
            { role: "dm", content: `[error: ${detail}]` },
          ]);
          setStreamingDm("");
        },
      });
      setTurnStatus(null);
    } catch (e) {
      setTurnStatus(null);
      setLines((prev) => [...prev, { role: "dm", content: `[network error: ${String(e)}]` }]);
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
        <Pill>{hpPill}</Pill>
        <Pill>{scenePill}</Pill>
        {turnStatus ? (
          <Pill className="italic opacity-85">{turnStatus}</Pill>
        ) : null}
      </header>

      {loadingHistory ? (
        <div className="flex flex-1 items-center px-4 py-6">
          <div className="rounded-lg bg-[#1f1f23] px-3 py-2.5">Loading...</div>
        </div>
      ) : (
        <MessageList messages={lines} streamingDm={streamingDm} />
      )}

      <ComposerBar value={draft} onChange={setDraft} onSend={() => void send()} disabled={busy} />
    </div>
  );
}
