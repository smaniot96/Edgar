import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";

import { ApiError, getCampaign, isNotFound, startSessionIntro } from "../api/client";
import { SourceRecovery } from "../components/campaign/SourceRecovery";
import { ReadinessNotice } from "../components/campaign/ReadinessNotice";
import { ComposerBar } from "../components/ComposerBar";
import { MessageList } from "../components/MessageList";
import { CampaignEndPanel } from "../components/play/CampaignEndPanel";
import { CharacterSheet } from "../components/play/CharacterSheet";
import { CombatHud } from "../components/play/CombatHud";
import { DevPipeline } from "../components/play/DevPipeline";
import { PlayRecap } from "../components/play/PlayRecap";
import { PlayIdeas } from "../components/play/PlayIdeas";
import { PlayHeader } from "../components/play/PlayHeader";
import { PlayToolBar, PlayToolPanel } from "../components/play/PlayTools";
import { linesFromHistory } from "../components/play/turnState";
import { useJournal } from "../components/journal/api";
import { usePlaySession } from "../components/play/usePlaySession";
import { useSessionDraft } from "../components/play/useSessionDraft";
import { useSessionMap } from "../components/play/useSessionMap";
import { useTurn } from "../components/play/useTurn";
import { useNarrowViewport } from "../components/play/useNarrowViewport";
import { usePlayWorkspaceState, type PlayTool } from "../components/play/usePlayWorkspaceState";
import { ButtonLink } from "../components/ui/Button";
import { EmptyState } from "../components/ui/EmptyState";
import { OfflineState } from "../components/ui/ErrorNotice";
import { useMode } from "../dev/ModeContext";

function journalSnapshot(data: import("../components/journal/types").JournalRead) {
  return new Map(data.entries.map((entry) => [
    entry.id,
    JSON.stringify([entry.status, entry.history.map((change) => [change.status, change.turn_number])]),
  ]));
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
  const [searchParams] = useSearchParams();
  const { isDev } = useMode();
  const game = usePlaySession(sessionId);
  const { applyTurnResult, setCampaignComplete, setSceneId, boot, campaignComplete, campaign, updateCampaign, refreshReadiness } = game;

  const {
    draft,
    revision: draftRevision,
    pendingRequest: pendingDraftRequest,
    edit: editDraft,
    clearIfRevision: clearDraftIfRevision,
    beginRequest: beginDraftRequest,
    clearRequest: clearDraftRequest,
  } = useSessionDraft(sessionId);
  const [workspace, setWorkspace] = usePlayWorkspaceState(sessionId, null);
  const activeTool = workspace.activeTool;
  const narrow = useNarrowViewport();
  const journal = useJournal(sessionId);
  const { data: sessionMapData, loading: sessionMapLoading, error: sessionMapError, refresh: refreshSessionMap } = useSessionMap(sessionId, activeTool === "map");
  const { data: journalData, refresh: refreshJournal, settledRevision: journalSettledRevision } = journal;
  const [introPending, setIntroPending] = useState(false);
  const [openEventId, setOpenEventId] = useState<number | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const composerRef = useRef<HTMLDivElement | null>(null);
  const historyOverlayRef = useRef(typeof window !== "undefined" && window.history.state?.__edgarPlayTool === true);
  const handledJournalLinkRef = useRef<string | null>(null);
  const knownRequestRef = useRef<string | null>(null);
  const recoveredRequestRef = useRef<string | null>(null);
  const pendingJournalFeedbackRef = useRef<{ before: Map<string, string>; afterRevision: number } | null>(null);
  const [journalFeedback, setJournalFeedback] = useState("");
  const [journalFeedbackEntryId, setJournalFeedbackEntryId] = useState<string | null>(null);

  useEffect(() => {
    if (searchParams.get("tool") !== "journal") return;
    const entryId = searchParams.get("entry");
    if (!entryId) return;
    const linkKey = `${sessionId}:${entryId}`;
    if (handledJournalLinkRef.current === linkKey) return;
    handledJournalLinkRef.current = linkKey;
    setWorkspace((current) => ({
      ...current,
      activeTool: "journal",
      query: "",
      category: "all",
      selectedEntryId: entryId,
    }));
  }, [searchParams, sessionId, setWorkspace]);

  const focusInput = useCallback(() => {
    requestAnimationFrame(() => {
      const ae = document.activeElement;
      const lost = !ae || ae === document.body || composerRef.current?.contains(ae);
      if (lost) inputRef.current?.focus();
    });
  }, []);

  const setActiveTool = useCallback((tool: PlayTool) => {
    if (workspace.activeTool !== null && tool === null && historyOverlayRef.current) {
      historyOverlayRef.current = false;
      window.history.back();
    }
    setWorkspace((current) => ({ ...current, activeTool: tool }));
  }, [setWorkspace, workspace.activeTool]);
  const toggleTool = useCallback((tool: Exclude<PlayTool, null>) => {
    const next = activeTool === tool ? null : tool;
    setActiveTool(next);
  }, [activeTool, setActiveTool]);
  const closeTool = useCallback(() => setActiveTool(null), [setActiveTool]);
  const openJournalEntry = useCallback((entryId: string) => {
    setActiveTool("journal");
    setWorkspace((current) => ({ ...current, activeTool: "journal", query: "", category: "all", selectedEntryId: entryId }));
  }, [setActiveTool, setWorkspace]);
  const resumeAdventure = useCallback(() => {
    closeTool();
    requestAnimationFrame(() => inputRef.current?.focus());
  }, [closeTool]);
  const prepareTravel = useCallback((place: string) => {
    const travelIdea = `I want to travel to ${place}.`;
    editDraft(draft.trim() ? `${draft.trim()}\n${travelIdea}` : travelIdea);
    closeTool();
    requestAnimationFrame(() => inputRef.current?.focus());
  }, [closeTool, draft, editDraft]);
  const openJournalHistory = useCallback((eventId: number) => {
    setOpenEventId(eventId);
    closeTool();
  }, [closeTool]);
  const clearOpenedHistory = useCallback((eventId: number) => {
    setOpenEventId((current) => current === eventId ? null : current);
  }, []);

  useEffect(() => {
    const onPopState = (event: PopStateEvent) => {
      if (!historyOverlayRef.current || event.state?.__edgarPlayTool === true) return;
      historyOverlayRef.current = false;
      setWorkspace((current) => ({ ...current, activeTool: null }));
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [setWorkspace]);

  useEffect(() => {
    if (!activeTool || historyOverlayRef.current) return;
    window.history.pushState({ ...(window.history.state ?? {}), __edgarPlayTool: true }, "", window.location.href);
    historyOverlayRef.current = true;
  }, [activeTool]);
  const handleCommittedResult = useCallback((result: import("../api/types").TurnResult) => {
    applyTurnResult(result);
    pendingJournalFeedbackRef.current = journalData
      ? { before: journalSnapshot(journalData), afterRevision: journalSettledRevision + 1 }
      : null;
    setJournalFeedback("");
    setJournalFeedbackEntryId(null);
    refreshJournal();
    refreshSessionMap();
  }, [applyTurnResult, journalData, refreshJournal, journalSettledRevision, refreshSessionMap]);
  const turn = useTurn(sessionId, {
    debug: isDev,
    onResult: handleCommittedResult,
    onRequestStarted: (request) => {
      knownRequestRef.current = request.requestId;
      beginDraftRequest(request);
    },
    onAcknowledged: (revision, requestId) => {
      clearDraftIfRevision(revision);
      clearDraftRequest(requestId);
    },
    onCampaignEnded: () => setCampaignComplete(true),
    onReadinessRejected: () => {
      void refreshReadiness();
      if (game.campaignId) void getCampaign(game.campaignId).then(updateCampaign).catch(() => {});
    },
    onSettled: focusInput,
  });
  const { dispatch, send, stop, retry, recover: recoverTurn, busy } = turn;
  const { lines, pending, announcement } = turn.state;
  const hasUncertainAction = lines.some((line) => line.role === "error" && line.uncertain);

  useEffect(() => {
    const pendingFeedback = pendingJournalFeedbackRef.current;
    if (!pendingFeedback || journal.loading || journal.settledRevision < pendingFeedback.afterRevision) return;
    pendingJournalFeedbackRef.current = null;
    if (journal.error || !journal.data) return;
    const changed = journal.data.entries.filter((entry) => {
      const after = JSON.stringify([entry.status, entry.history.map((change) => [change.status, change.turn_number])]);
      return pendingFeedback.before.get(entry.id) !== after;
    });
    const removed = [...pendingFeedback.before.keys()].filter((id) => !journal.data!.entries.some((entry) => entry.id === id));
    if (!changed.length && !removed.length) return;
    const titles = [...changed.map((entry) => entry.title), ...removed.map(() => "an entry")].slice(0, 2);
    const suffix = changed.length + removed.length > titles.length ? ` and ${changed.length + removed.length - titles.length} more` : "";
    setJournalFeedbackEntryId(changed[0]?.id ?? null);
    setJournalFeedback(`Journal updated: ${titles.join(", ")}${suffix}.`);
  }, [journal.data, journal.error, journal.loading, journal.settledRevision]);

  // History seeding depends only on boot, so changing source readiness preserves live chat.
  useEffect(() => {
    if (boot) dispatch({ type: "reset", lines: linesFromHistory(boot.history) });
  }, [boot, dispatch]);

  // A browser can close after the server commits but before the stream acknowledges it.
  // Resume by checking the durable receipt before offering a retry or another action.
  useEffect(() => {
    const request = pendingDraftRequest;
    if (!boot || !turn.state.historyReady || !request || busy || knownRequestRef.current === request.requestId || recoveredRequestRef.current === request.requestId) return;
    recoveredRequestRef.current = request.requestId;
    void recoverTurn(request);
  }, [boot, busy, pendingDraftRequest, recoverTurn, turn.state.historyReady]);

  const introRequested = useRef(false);
  useEffect(() => {
    if (!boot || !campaign) return;
    if (boot.history.length > 0 || boot.campaignEnded || game.readiness?.play_allowed !== true || introRequested.current) return;
    introRequested.current = true;
    setIntroPending(true);
    (async () => {
      try {
        const data = await startSessionIntro(sessionId);
        if (data.started && data.narration) {
          dispatch({ type: "append", line: { id: "intro", role: "dm", content: data.narration } });
          if (data.current_scene_id !== undefined) setSceneId(data.current_scene_id);
        }
      } catch (error) {
        void refreshReadiness();
        if (error instanceof ApiError) {
          let detail;
          try { detail = JSON.parse(error.body)?.detail; } catch { /* non-JSON failure */ }
          if (detail?.code === "campaign_sources_unavailable") {
            updateCampaign({ ...campaign, sources: detail.sources, retrieval_degraded: true, play_allowed: false });
            introRequested.current = false;
          }
        }
      } finally {
        setIntroPending(false);
      }
    })();
  }, [boot, sessionId, dispatch, setSceneId, campaign, updateCampaign, refreshReadiness, game.readiness?.play_allowed]);

  const handleSend = useCallback(() => {
    const msg = draft.trim();
    if (!msg || busy || hasUncertainAction || campaignComplete || game.readiness?.play_allowed !== true) return;
    void send(msg, draftRevision);
  }, [draft, draftRevision, busy, hasUncertainAction, campaignComplete, send, game.readiness?.play_allowed]);

  const handleQuickReply = useCallback(
    (text: string) => {
      if (busy || hasUncertainAction || campaignComplete || game.readiness?.play_allowed !== true) return;
      void send(text);
    },
    [busy, hasUncertainAction, campaignComplete, send, game.readiness?.play_allowed],
  );

  const handleRetry = useCallback(
    (turnId: string, input: string, requestId?: string, draftRevision?: number) => {
      retry(turnId, input, requestId, draftRevision);
    },
    [retry],
  );

  const handleReopened = useCallback(() => {
    setCampaignComplete(false);
    void refreshReadiness();
    dispatch({
      type: "append",
      line: { id: `reopen-${Date.now()}`, role: "notice", content: "The campaign has been reopened. The adventure continues…" },
    });
    focusInput();
  }, [dispatch, focusInput, refreshReadiness, setCampaignComplete]);

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
  const clueEntriesByTitle = new Map<string, string[]>();
  for (const entry of journal.data?.entries ?? []) {
    if (entry.category !== "clue" || !entry.text?.trim()) continue;
    const key = entry.title;
    clueEntriesByTitle.set(key, [...(clueEntriesByTitle.get(key) ?? []), entry.id]);
  }
  const readableItems = new Map([...clueEntriesByTitle.entries()]
    .filter(([, ids]) => ids.length === 1)
    .map(([title, ids]) => [title, ids[0]]));

  return (
    <div className={SCREEN}>
      <div className="flex min-h-0 flex-1">
        <div
          className="flex min-h-0 min-w-0 flex-1 flex-col"
          inert={narrow && activeTool !== null}
          aria-hidden={narrow && activeTool !== null ? true : undefined}
        >
          <PlayHeader
            title={game.title}
            campaignId={game.campaignId}
            character={character}
            sheetOpen={activeTool === "character"}
            onToggleSheet={() => toggleTool("character")}
            isDev={isDev}
            sessionId={sessionId}
            sceneId={game.sceneId}
          />
          <ReadinessNotice readiness={game.readiness} onRetry={game.refreshReadiness} announce={false} className="mx-3 mt-2 [overflow-wrap:anywhere] sm:mx-4" />
          {game.campaign ? <SourceRecovery campaign={game.campaign} onUpdated={(updated) => {
            game.updateCampaign(updated);
            void game.refreshReadiness();
          }} /> : null}
          {isDev ? <DevPipeline stage={pending?.stage ?? null} /> : null}
          <PlayRecap
            data={journal.data}
            loading={journal.loading}
            error={journal.error}
            onOpenEntry={openJournalEntry}
            onResume={resumeAdventure}
          />
          <div role="group" aria-label="Current adventure context" className="mx-3 mb-2 flex shrink-0 flex-wrap gap-x-4 gap-y-1 rounded-control bg-surface/60 px-3 py-2 text-xs [overflow-wrap:anywhere] sm:mx-4">
            <p className="min-w-0 whitespace-normal break-words"><span className="font-semibold text-ink-subtle">Place: </span>{journal.data?.current_place ? <button type="button" className="min-h-11 max-w-full whitespace-normal break-words text-left text-ember-300 underline decoration-ember-400 underline-offset-2 [overflow-wrap:anywhere]" onClick={() => openJournalEntry(journal.data!.current_place!.id)}>{journal.data.current_place.title}</button> : <span className="text-ink-muted">Unknown</span>}</p>
            <p className="min-w-0 whitespace-normal break-words"><span className="font-semibold text-ink-subtle">Goal: </span>{journal.data?.active_goal ? <button type="button" className="min-h-11 max-w-full whitespace-normal break-words text-left text-ember-300 underline decoration-ember-400 underline-offset-2 [overflow-wrap:anywhere]" onClick={() => openJournalEntry(journal.data!.active_goal!.id)}>{journal.data.active_goal.title}</button> : <span className="text-ink-muted">No active goal recorded</span>}</p>
          </div>
          <PlayIdeas
            data={journal.data}
            hasDraft={draft.trim().length > 0}
            onAppend={(idea) => editDraft(`${draft}${draft && !draft.endsWith("\n") ? "\n" : ""}${idea}`)}
          />
          <PlayToolBar activeTool={activeTool} onToggle={toggleTool} />
          {journalFeedback ? (
            <p role="status" className="mx-3 my-2 flex shrink-0 items-center justify-between gap-3 rounded-control bg-success-soft px-3 py-2 text-sm text-success sm:mx-4">
              <span>{journalFeedback}</span>
              <button type="button" className="shrink-0 font-medium underline underline-offset-2" onClick={() => { if (journalFeedbackEntryId) openJournalEntry(journalFeedbackEntryId); else setActiveTool("journal"); setJournalFeedback(""); }}>Open Journal</button>
            </p>
          ) : null}

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
                  actorEntries={journal.data?.entries.filter((entry) => entry.category === "person")}
                  onOpenActorEntry={openJournalEntry}
                  messages={lines}
                  pending={
                    pending ??
                    (introPending
                      ? { turnId: "intro", requestId: "intro", input: "", stage: null, text: "", adjudication: null, debug: null }
                      : null)
                  }
                  onRetry={handleRetry}
                  onCheckReceipt={turn.checkReceipt}
                  canRetry={!busy}
                  layoutKey={activeTool ?? "none"}
                  storageKey={`edgar_play_anchor:v1:${sessionId}`}
                  restoreReady={!game.loading && !!boot && turn.state.historyReady}
                  openEventId={openEventId}
                  onHistoryOpened={clearOpenedHistory}
                  onQuickReply={campaignComplete || game.readiness?.play_allowed !== true ? undefined : handleQuickReply}
                />
              )}

              <div ref={composerRef} className="shrink-0">
                {!campaignComplete ? (
                  <ReadinessNotice readiness={game.readiness} onRetry={game.refreshReadiness} announce={false} className="mx-3 mb-2 [overflow-wrap:anywhere] sm:mx-4" />
                ) : null}
                {campaignComplete ? (
                  <CampaignEndPanel campaignId={game.campaignId} onReopened={handleReopened} />
                ) : (
                  <ComposerBar
                    inputRef={inputRef}
                    value={draft}
                    onChange={editDraft}
                    onSend={handleSend}
                    onStop={stop}
                    busy={busy}
                    canStop={pending?.stage !== "saving"}
                    allowSend={!hasUncertainAction}
                    disabled={game.loading || introPending || game.readiness?.play_allowed !== true}
                  />
                )}
              </div>
            </main>
          </div>
        </div>

        <CharacterSheet
          character={character}
          open={activeTool === "character"}
          onClose={closeTool}
          readableItems={readableItems}
          onReadItem={openJournalEntry}
        />
        <PlayToolPanel
          activeTool={activeTool}
          journal={{ data: journal.data, loading: journal.loading, error: journal.error, stale: journal.stale }}
          query={workspace.query}
          onQueryChange={(query) => setWorkspace((current) => ({ ...current, query }))}
          category={workspace.category}
          onCategoryChange={(category) => setWorkspace((current) => ({ ...current, category }))}
          selectedEntryId={workspace.selectedEntryId}
          onSelectedEntryChange={(selectedEntryId) => setWorkspace((current) => ({ ...current, selectedEntryId }))}
          onRefresh={journal.refresh}
          onClose={closeTool}
          onOpenHistory={openJournalHistory}
          sessionId={sessionId}
          map={sessionMapData}
          mapLoading={sessionMapLoading}
          mapError={sessionMapError}
          onMapRefresh={refreshSessionMap}
          onOpenMapJournalEntry={openJournalEntry}
          onPrepareTravel={prepareTravel}
        />
      </div>

      {/* Single polite live region: announces completed narration / errors, not every token. */}
      <div role="status" aria-live="polite" aria-atomic="true" className="sr-only">
        {announcement || (game.readiness?.play_allowed === false
          ? game.readiness.blockers.map((blocker) => blocker.message).join(" ")
          : "")}
      </div>
    </div>
  );
}
