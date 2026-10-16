import { useCallback, useEffect, useRef, useState } from "react";

import {
  getAdventure,
  getCampaign,
  getCharacter,
  getSession,
  getSessionCombat,
  listCharacterAssignments,
  listSessionMessages,
} from "../../api/client";
import type {
  CampaignRead,
  ChatMessageRead,
  CombatState,
  ReadinessRead,
  TurnResult,
} from "../../api/types";
import { campaignDisplayName, isGenericCampaignTitle } from "../../lib/campaign";
import { rememberLastSession } from "../../lib/lastSession";
import { mergeTurnCharacter, sheetForCampaign, type SheetCharacter } from "./gameState";

export interface PlaySessionState {
  loading: boolean;
  /** Boot failure (ApiError etc.); render with ErrorNotice/OfflineState. */
  error: unknown;
  title: string;
  campaignId: number | null;
  campaign: CampaignRead | null;
  readiness: ReadinessRead | null;
  campaignComplete: boolean;
  character: SheetCharacter | null;
  sceneId: string | null;
  combat: CombatState | null;
  /** Snapshot taken once at boot (null until loaded): chat history + whether the campaign had ended. */
  boot: { history: ChatMessageRead[]; campaignEnded: boolean } | null;
}

const initial: PlaySessionState = {
  loading: true,
  error: null,
  title: "Edgar",
  campaignId: null,
  campaign: null,
  readiness: null,
  campaignComplete: false,
  character: null,
  sceneId: null,
  combat: null,
  boot: null,
};

const READINESS_UNKNOWN: ReadinessRead = {
  play_allowed: false,
  history_allowed: true,
  state: "readiness_unavailable",
  primary_action: "retry_readiness",
  blockers: [{
    code: "readiness_unavailable",
    message: "Campaign readiness could not be checked. Retry before starting a turn.",
    action: "retry_readiness",
  }],
};

/** Active, un-ended combat for the session; null if none or the endpoint isn't available. */
async function loadCombat(sessionId: number, signal: AbortSignal): Promise<CombatState | null> {
  try {
    const c = await getSessionCombat(sessionId, signal);
    return c && Array.isArray(c.combatants) && !c.ended ? c : null;
  } catch {
    return null; // endpoint missing (404) or no active combat
  }
}

/** Player-facing campaign name (the adventure's title when the campaign title is generic). */
async function loadTitle(campaign: CampaignRead | null, signal: AbortSignal): Promise<string> {
  if (!campaign) return "Edgar";
  let adventure = null;
  const slug = campaign.adventure_collections[0];
  if (slug && isGenericCampaignTitle(campaign)) adventure = await getAdventure(slug, signal).catch(() => null);
  return campaignDisplayName(campaign, adventure);
}

async function loadCharacter(
  characterId: number,
  campaignId: number,
  signal: AbortSignal,
): Promise<SheetCharacter | null> {
  const [c, assigns] = await Promise.all([
    getCharacter(characterId, signal),
    listCharacterAssignments(characterId, signal).catch(() => []),
  ]);
  return sheetForCampaign(c, assigns, campaignId);
}

/** Loads everything the play screen needs for a session and applies post-turn updates. */
export function usePlaySession(sessionId: number) {
  const [state, setState] = useState<PlaySessionState>(initial);
  const [attempt, setAttempt] = useState(0);
  const readinessRevision = useRef(0);

  useEffect(() => {
    if (!Number.isFinite(sessionId) || sessionId < 1) return;
    const ctrl = new AbortController();
    const revision = ++readinessRevision.current;
    (async () => {
      try {
        const session = await getSession(sessionId, ctrl.signal);
        const [campaign, character, combat, history] = await Promise.all([
          getCampaign(session.campaign_id, ctrl.signal),
          session.active_character_id
            ? loadCharacter(session.active_character_id, session.campaign_id, ctrl.signal).catch(() => null)
            : Promise.resolve(null),
          loadCombat(sessionId, ctrl.signal),
          listSessionMessages(sessionId, ctrl.signal),
        ]);
        const title = await loadTitle(campaign, ctrl.signal);
        if (ctrl.signal.aborted || revision !== readinessRevision.current) return;
        rememberLastSession(sessionId);
        setState({
          loading: false,
          error: null,
          title,
          campaignId: session.campaign_id,
          campaign,
          readiness: session.readiness ?? campaign?.readiness ?? READINESS_UNKNOWN,
          campaignComplete: campaign?.status === "ended",
          character,
          sceneId: session.current_scene_id,
          combat,
          boot: { history, campaignEnded: campaign?.status === "ended" },
        });
      } catch (e) {
        if (ctrl.signal.aborted) return;
        setState({ ...initial, loading: false, error: e });
      }
    })();
    return () => ctrl.abort();
  }, [sessionId, attempt]);

  /** Re-run the boot fetch (after an error). */
  const reload = useCallback(() => {
    readinessRevision.current += 1;
    setState(initial);
    setAttempt((n) => n + 1);
  }, []);

  const refreshReadiness = useCallback(async () => {
    const revision = ++readinessRevision.current;
    try {
      const session = await getSession(sessionId);
      if (revision !== readinessRevision.current) return;
      setState((s) => ({
        ...s,
        readiness: session.readiness ?? READINESS_UNKNOWN,
        campaignComplete: s.campaignComplete || session.readiness?.state === "completed",
      }));
    } catch {
      if (revision === readinessRevision.current) {
        setState((s) => ({ ...s, readiness: READINESS_UNKNOWN }));
      }
    }
  }, [sessionId]);

  /** Apply a `done` payload (character/HP, scene, combat, campaign end). */
  const applyTurnResult = useCallback((r: TurnResult) => {
    setState((s) => ({
      ...s,
      character: r.character ? mergeTurnCharacter(s.character, r.character) : s.character,
      sceneId: r.current_scene_id !== undefined ? r.current_scene_id : s.sceneId,
      // Hide the HUD when combat ends; keep it while combat is ongoing.
      combat: r.combat_state && !r.combat_state.ended ? r.combat_state : null,
      campaignComplete: s.campaignComplete || !!r.campaign_complete,
      readiness: r.campaign_complete
        ? {
            play_allowed: false,
            history_allowed: true,
            state: "completed",
            primary_action: "read_history",
            blockers: [{ code: "completed", message: "This campaign is complete. Read the saved story or reopen the campaign to continue.", action: "read_history" }],
          }
        : s.readiness,
    }));
  }, []);

  const setCampaignComplete = useCallback((v: boolean) => {
    setState((s) => ({
      ...s,
      campaignComplete: v,
      readiness: v ? {
        play_allowed: false,
        history_allowed: true,
        state: "completed",
        primary_action: "read_history",
        blockers: [{ code: "completed", message: "This campaign is complete. Read the saved story or reopen the campaign to continue.", action: "read_history" }],
      } : s.readiness,
    }));
  }, []);

  const setSceneId = useCallback((sceneId: string | null) => {
    setState((s) => ({ ...s, sceneId }));
  }, []);

  const updateCampaign = useCallback((campaign: CampaignRead) => {
    setState((s) => ({ ...s, campaign }));
  }, []);

  return { ...state, applyTurnResult, setCampaignComplete, setSceneId, reload, refreshReadiness, updateCampaign };
}
