import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { apiFetch, isNotFound } from "../api/client";
import { ComposerBar } from "../components/ComposerBar";
import { MessageList } from "../components/MessageList";
import { CampaignEndPanel } from "../components/play/CampaignEndPanel";
import { CharacterSheet } from "../components/play/CharacterSheet";
import { CombatHud } from "../components/play/CombatHud";
import { DevPipeline } from "../components/play/DevPipeline";
import { PlayHeader } from "../components/play/PlayHeader";
import { linesFromHistory } from "../components/play/turnState";
import { usePlaySession } from "../components/play/usePlaySession";
import { useTurn } from "../components/play/useTurn";
import { ButtonLink } from "../components/ui/Button";
import { EmptyState } from "../components/ui/EmptyState";
import { OfflineState } from "../components/ui/ErrorNotice";
import { useMode } from "../dev/ModeContext";

const SHEET_PREF_KEY = "edgar_sheet_open";

function initialSheetOpen(): boolean {
  try {
    const v = localStorage.getItem(SHEET_PREF_KEY);
    if (v === "1") return true;
    if (v === "0") return false;
  } catch {
    /* storage unavailable */
  }
  return typeof window !== "undefined" && window.matchMedia?.("(min-width: 1024px)").matches === true;
}

/**
 * Full-height play screen: fixed header, scrolling log, composer pinned to the bottom.
 * Fills the routed `flex-1 min-h-0` area of the `h-dvh` app shell; only the log scrolls.
 */
const SCREEN = "flex h-full min-h-0 flex-1 flex-col text-ink";

export default function Play() {
  const { sessionId: param } = useParams<{ sessionId: string }>();
  const sessionId = Number(param);
  if (!Number.isInteger(sessionId) || sessionId < 1) {
    return <PlayNotFound />;
  }
  // Remount per session so turn state, aborts and boot all reset cleanly.
  return <PlayScreen key={sessionId} sessionId={sessionId} />;
}

function PlayNotFound() {
  return (
    <div className={`${SCREEN} items-center justify-center p-6`}>
      <EmptyState
        title="This adventure session doesn't exist."
        action={<ButtonLink to="/campaigns">Back to campaigns</ButtonLink>}
      >
        It may have been deleted, or the link is wrong.
      </EmptyState>
    </div>
  );
}

function PlayScreen({ sessionId }: { sessionId: number }) {
  const { isDev } = useMode();
  const game = usePlaySession(sessionId);
  const { applyTurnResult, setCampaignComplete, setSceneId, boot, campaignComplete } = game;

  const [draft, setDraft] = useState("");
  const [sheetOpen, setSheetOpen] = useState(initialSheetOpen);
  const [introPending, setIntroPending] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const composerRef = useRef<HTMLDivElement | null>(null);

  const focusInput = useCallback(() => {
    requestAnimationFrame(() => {
      const ae = document.activeElement;
      const lost = !ae || ae === document.body || composerRef.current?.contains(ae);
      if (lost) inputRef.current?.focus();
    });
  }, []);

  const turn = useTurn(sessionId, {
    debug: isDev,
    onResult: applyTurnResult,
    onCampaignEnded: () => setCampaignComplete(true),
    onFailed: (input) => setDraft((d) => (d.trim() ? d : input)),
    onSettled: focusInput,
  });
  const { dispatch, send, stop, retry, busy } = turn;
  const { lines, pending, announcement } = turn.state;

  // Seed the transcript from history once loaded; open with a DM scene on an empty session.
  const introRequested = useRef(false);
  useEffect(() => {
    if (!boot) return;
    dispatch({ type: "reset", lines: linesFromHistory(boot.history) });
    if (boot.history.length > 0 || boot.campaignEnded || introRequested.current) return;
    introRequested.current = true;
    setIntroPending(true);
    (async () => {
      try {
        const r = await apiFetch(`/api/sessions/${sessionId}/intro`, { method: "POST" });
        const data = (await r.json()) as {
          started: boolean;
          narration?: string;
          current_scene_id?: string | null;
        };
        if (data.started && data.narration) {
          dispatch({ type: "append", line: { id: "intro", role: "dm", content: data.narration } });
          if (data.current_scene_id !== undefined) setSceneId(data.current_scene_id);
        }
      } catch {
        /* non-fatal: the player can still type a first action */
      } finally {
        setIntroPending(false);
      }
    })();
  }, [boot, sessionId, dispatch, setSceneId]);

  const toggleSheet = useCallback(() => {
    setSheetOpen((o) => {
      try {
        localStorage.setItem(SHEET_PREF_KEY, o ? "0" : "1");
      } catch {
        /* best-effort */
      }
      return !o;
    });
  }, []);
  const closeSheet = useCallback(() => {
    setSheetOpen(false);
    try {
      localStorage.setItem(SHEET_PREF_KEY, "0");
    } catch {
      /* best-effort */
    }
  }, []);

  const handleSend = useCallback(() => {
    const msg = draft.trim();
    if (!msg || busy || campaignComplete) return;
    setDraft("");
    void send(msg);
  }, [draft, busy, campaignComplete, send]);

  const handleQuickReply = useCallback(
    (text: string) => {
      if (busy || campaignComplete) return;
      void send(text);
    },
    [busy, campaignComplete, send],
  );

  const handleRetry = useCallback(
    (turnId: string, input: string) => {
      // The failed input was restored to the composer; don't leave a duplicate there.
      setDraft((d) => (d.trim() === input ? "" : d));
      retry(turnId, input);
    },
    [retry],
  );

  const handleReopened = useCallback(() => {
    setCampaignComplete(false);
    dispatch({
      type: "append",
      line: { id: `reopen-${Date.now()}`, role: "notice", content: "The campaign has been reopened. The adventure continues…" },
    });
    focusInput();
  }, [dispatch, focusInput, setCampaignComplete]);

  if (game.error) {
    if (isNotFound(game.error)) return <PlayNotFound />;
    return (
      <div className={`${SCREEN} items-center justify-center p-6`}>
        <OfflineState error={game.error} onRetry={game.reload} title="Couldn't load this session" />
      </div>
    );
  }

  // The engine reports the player's AC on combat state; use it when the sheet lacks one.
  const playerAc = game.combat?.player_ac;
  const character =
    game.character && game.character.ac == null && typeof playerAc === "number"
      ? { ...game.character, ac: playerAc }
      : game.character;

  return (
    <div className={SCREEN}>
      <PlayHeader
        title={game.title}
        campaignId={game.campaignId}
        character={character}
        sheetOpen={sheetOpen}
        onToggleSheet={toggleSheet}
        isDev={isDev}
        sessionId={sessionId}
        sceneId={game.sceneId}
      />
      {isDev ? <DevPipeline stage={pending?.stage ?? null} /> : null}

      <div className="flex min-h-0 flex-1">
        <main className="flex min-h-0 min-w-0 flex-1 flex-col">
          {game.combat ? <CombatHud combat={game.combat} /> : null}

          {game.loading ? (
            <div className="flex flex-1 items-center justify-center">
              <p className="animate-pulse font-serif italic text-ink-muted motion-reduce:animate-none">
                Unrolling the map…
              </p>
            </div>
          ) : (
            <MessageList
              messages={lines}
              pending={
                pending ??
                (introPending
                  ? { turnId: "intro", input: "", stage: null, text: "", adjudication: null, debug: null }
                  : null)
              }
              onRetry={handleRetry}
              canRetry={!busy && !campaignComplete}
              forceScrollKey={pending?.turnId ?? null}
              onQuickReply={campaignComplete ? undefined : handleQuickReply}
            />
          )}

          <div ref={composerRef} className="shrink-0">
            {campaignComplete ? (
              <CampaignEndPanel campaignId={game.campaignId} onReopened={handleReopened} />
            ) : (
              <ComposerBar
                inputRef={inputRef}
                value={draft}
                onChange={setDraft}
                onSend={handleSend}
                onStop={stop}
                busy={busy}
                canStop={pending?.stage !== "saving"}
                disabled={game.loading || introPending}
              />
            )}
          </div>
        </main>

        <CharacterSheet character={character} open={sheetOpen} onClose={closeSheet} />
      </div>

      {/* Single polite live region: announces completed narration / errors, not every token. */}
      <div role="status" aria-live="polite" aria-atomic="true" className="sr-only">
        {announcement}
      </div>
    </div>
  );
}
