import { useCallback, useEffect, useState } from "react";

import { apiFetch, getAdventure, getCampaign, getSession } from "../../api/client";
import type {
  CampaignRead,
  CharacterAssignmentHistoryRead,
  CharacterRead,
  ChatMessageRead,
  CombatState,
  SessionRead,
  TurnResult,
} from "../../api/types";
import { campaignDisplayName, isGenericCampaignTitle } from "../../lib/campaign";
import { rememberLastSession } from "../../lib/lastSession";
import { mergeTurnCharacter, sheetFromLibrary, type SheetCharacter } from "./gameState";

export interface PlaySessionState {
  loading: boolean;
  /** Boot failure (ApiError etc.); render with ErrorNotice/OfflineState. */
  error: unknown;
  title: string;
  campaignId: number | null;
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
  campaignComplete: false,
  character: null,
  sceneId: null,
  combat: null,
  boot: null,
};

async function getJson<T>(path: string): Promise<T> {
  return (await (await apiFetch(path)).json()) as T;
}

/** Active, un-ended combat for the session; null if none or the endpoint isn't available. */
async function loadCombat(sessionId: number, session: SessionRead): Promise<CombatState | null> {
  const embedded = (session as SessionRead & { combat_state?: CombatState | null }).combat_state;
  if (embedded !== undefined) return embedded && !embedded.ended ? embedded : null;
  try {
    const c = await getJson<CombatState | null>(`/api/sessions/${sessionId}/combat`);
    return c && Array.isArray(c.combatants) && !c.ended ? c : null;
  } catch {
    return null; // endpoint missing (404) or no active combat
  }
}

/** Player-facing campaign name (the adventure's title when the campaign title is generic). */
async function loadTitle(campaign: CampaignRead | null): Promise<string> {
  if (!campaign) return "Edgar";
  let adventure = null;
  const slug = campaign.adventure_collections[0];
  if (slug && isGenericCampaignTitle(campaign)) adventure = await getAdventure(slug).catch(() => null);
  return campaignDisplayName(campaign, adventure);
}

async function loadCharacter(
  characterId: number,
  campaignId: number,
): Promise<SheetCharacter | null> {
  const [c, assigns] = await Promise.all([
    getJson<CharacterRead>(`/api/characters/${characterId}`),
    getJson<CharacterAssignmentHistoryRead[]>(`/api/characters/${characterId}/assignments`).catch(
      () => [] as CharacterAssignmentHistoryRead[],
    ),
  ]);
  const active =
    assigns.find((a) => a.campaign_id === campaignId && a.status === "active") ??
    assigns.find((a) => a.status === "active") ??
    null;
  return sheetFromLibrary(c, active);
}

/** Loads everything the play screen needs for a session and applies post-turn updates. */
export function usePlaySession(sessionId: number) {
  const [state, setState] = useState<PlaySessionState>(initial);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!Number.isFinite(sessionId) || sessionId < 1) return;
    let cancelled = false;
    (async () => {
      try {
        const session = await getSession(sessionId);
        const [campaign, character, combat, history] = await Promise.all([
          getCampaign(session.campaign_id).catch(() => null),
          session.active_character_id
            ? loadCharacter(session.active_character_id, session.campaign_id).catch(() => null)
            : Promise.resolve(null),
          loadCombat(sessionId, session),
          getJson<ChatMessageRead[]>(`/api/sessions/${sessionId}/messages`),
        ]);
        const title = await loadTitle(campaign);
        if (cancelled) return;
        rememberLastSession(sessionId);
        setState({
          loading: false,
          error: null,
          title,
          campaignId: session.campaign_id,
          campaignComplete: campaign?.status === "ended",
          character,
          sceneId: session.current_scene_id,
          combat,
          boot: { history, campaignEnded: campaign?.status === "ended" },
        });
      } catch (e) {
        if (cancelled) return;
        setState({ ...initial, loading: false, error: e });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sessionId, attempt]);

  /** Re-run the boot fetch (after an error). */
  const reload = useCallback(() => {
    setState(initial);
    setAttempt((n) => n + 1);
  }, []);

  /** Apply a `done` payload (character/HP, scene, combat, campaign end). */
  const applyTurnResult = useCallback((r: TurnResult) => {
    setState((s) => ({
      ...s,
      character: r.character ? mergeTurnCharacter(s.character, r.character) : s.character,
      sceneId: r.current_scene_id !== undefined ? r.current_scene_id : s.sceneId,
      // Hide the HUD when combat ends; keep it while combat is ongoing.
      combat: r.combat_state && !r.combat_state.ended ? r.combat_state : null,
      campaignComplete: s.campaignComplete || !!r.campaign_complete,
    }));
  }, []);

  const setCampaignComplete = useCallback((v: boolean) => {
    setState((s) => ({ ...s, campaignComplete: v }));
  }, []);

  const setSceneId = useCallback((sceneId: string | null) => {
    setState((s) => ({ ...s, sceneId }));
  }, []);

  return { ...state, applyTurnResult, setCampaignComplete, setSceneId, reload };
}
