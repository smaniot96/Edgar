/**
 * Campaign hub: header (name, status, lifecycle actions) + tabs
 * Sessions | Characters | NPCs | World state (one file each under ./campaign/).
 * The active tab lives in `?tab=` so it survives reloads and can be linked.
 */

import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import {
  deleteCampaign,
  endCampaign,
  getAdventure,
  getCampaign,
  isNotFound,
  listSessions,
  reopenCampaign,
  setCampaignDeathMode,
  setCampaignProgressionMode,
  setCampaignRulesEdition,
} from "../api/client";
import { SourceRecovery } from "../components/campaign/SourceRecovery";
import { ReadinessNotice } from "../components/campaign/ReadinessNotice";
import { AdventureReview } from "../components/campaign/AdventureReview";
import { Pill } from "../components/Pill";
import { Button, ButtonLink } from "../components/ui/Button";
import { Field } from "../components/ui/Field";
import { controlClasses } from "../components/ui/styles";
import { EmptyState, LoadingText } from "../components/ui/EmptyState";
import { ErrorNotice, OfflineState } from "../components/ui/ErrorNotice";
import { ArrowLeftIcon } from "../components/ui/icons";
import { SavedPlaythroughContext } from "../components/campaign/SavedPlaythroughContext";
import { Page } from "../components/ui/Page";
import { TabPanel, Tabs } from "../components/ui/Tabs";
import { useMode } from "../dev/ModeContext";
import { campaignDisplayName } from "../lib/campaign";
import { dateTime, relativeTime } from "../lib/format";
import { selectCampaignSession } from "../lib/campaignCards";
import { rememberLastSession } from "../lib/lastSession";
import { useQuery } from "../lib/useQuery";
import CharactersTab from "./campaign/CharactersTab";
import NpcsTab from "./campaign/NpcsTab";
import SessionsTab from "./campaign/SessionsTab";
import WorldTab from "./campaign/WorldTab";

const TABS = [
  { id: "sessions", label: "Sessions" },
  { id: "characters", label: "Characters" },
  { id: "npcs", label: "NPCs" },
  { id: "world", label: "World state" },
] as const;
type TabId = (typeof TABS)[number]["id"];

function isTab(v: string | null): v is TabId {
  return TABS.some((t) => t.id === v);
}

function BackLink() {
  return (
    <Link
      to="/campaigns"
      className="inline-flex items-center gap-1 rounded-sm text-ember-300 hover:text-ember-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-400"
    >
      <ArrowLeftIcon className="h-3.5 w-3.5" /> Campaigns
    </Link>
  );
}

export default function CampaignView() {
  const { id } = useParams<{ id: string }>();
  const campaignId = Number(id);
  const validId = Number.isInteger(campaignId) && campaignId > 0;
  const navigate = useNavigate();
  const { isDev } = useMode();
  const [params, setParams] = useSearchParams();
  const rawTab = params.get("tab");
  const tab: TabId = isTab(rawTab) ? rawTab : "sessions";

  const campaign = useQuery(validId ? `campaign:${campaignId}` : null, (s) =>
    getCampaign(campaignId, s),
  );
  const slug = campaign.data?.adventure_collections[0] ?? null;
  // Best-effort: only used for the display name / subtitle.
  const adventure = useQuery(slug ? `adventure:${slug}` : null, (s) => getAdventure(slug!, s));
  const sessions = useQuery(validId ? `sessions:${campaignId}` : null, (s) => listSessions(campaignId, s));

  const [busy, setBusy] = useState<"end" | "reopen" | "delete" | null>(null);
  const [actionError, setActionError] = useState<unknown>(null);

  function selectTab(next: TabId) {
    setParams(
      (prev) => {
        const p = new URLSearchParams(prev);
        if (next === "sessions") p.delete("tab");
        else p.set("tab", next);
        return p;
      },
      { replace: true },
    );
  }

  if (!validId || isNotFound(campaign.error)) {
    return (
      <Page width="md">
        <EmptyState
          title="Campaign not found"
          action={<ButtonLink to="/campaigns">Back to Campaigns</ButtonLink>}
        >
          It may have been deleted.
        </EmptyState>
      </Page>
    );
  }

  if (campaign.error && campaign.data === undefined) {
    return (
      <Page width="md">
        <OfflineState error={campaign.error} onRetry={campaign.refetch} />
      </Page>
    );
  }

  const c = campaign.data;
  if (!c) {
    return (
      <Page width="md">
        <LoadingText>Loading campaign…</LoadingText>
      </Page>
    );
  }

  const ended = c.status === "ended";
  const name = campaignDisplayName(c, adventure.data);
  const adventureTitle = adventure.data?.title;
  const savedSession = selectCampaignSession(sessions.data, c.id);
  const canContinue = savedSession?.readiness?.play_allowed === true;
  const canReadStory = savedSession?.has_history === true && savedSession.readiness?.history_allowed === true;
  const canStartSession = !ended && c.readiness?.play_allowed === true;

  async function run(kind: "end" | "reopen" | "delete") {
    if (!c) return;
    if (
      kind === "end" &&
      !window.confirm(
        `End "${name}"? Its characters are freed for other campaigns. You can reopen it later.`,
      )
    )
      return;
    if (kind === "delete" && !window.confirm(`Delete "${name}" and all its sessions? This cannot be undone.`))
      return;
    setBusy(kind);
    setActionError(null);
    try {
      if (kind === "delete") {
        await deleteCampaign(c.id);
        navigate("/campaigns");
        return;
      }
      const updated = kind === "end" ? await endCampaign(c.id) : await reopenCampaign(c.id);
      campaign.setData(() => updated);
    } catch (e) {
      setActionError(e);
    } finally {
      setBusy(null);
    }
  }

  return (
    <Page width="md">
      <header className="space-y-3">
        <div className="text-sm">
          <BackLink />
        </div>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="min-w-0 max-w-full break-words [overflow-wrap:anywhere] font-display text-display-md">{name}</h1>
              {ended ? <Pill>Ended</Pill> : <Pill tone="success">Active</Pill>}
            </div>
            <p className="mt-0.5 text-sm text-ink-muted">
              {adventureTitle && adventureTitle !== name ? `${adventureTitle} · ` : ""}
              {c.system}
              {ended && c.ended_at ? ` · ended ${dateTime(c.ended_at)}` : ""}
            </p>
            {isDev ? (
              <p className="mt-0.5 font-mono text-[11px] text-ink-subtle">
                campaign #{c.id} · title “{c.title}” · {c.adventure_collections.join(", ") || "no adventure"}
              </p>
            ) : null}
          </div>
        </div>
        <ErrorNotice error={actionError} />
      </header>

      <section aria-labelledby="saved-playthrough-title" className="rounded-card border border-line bg-surface-raised p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 id="saved-playthrough-title" className="font-display text-xl">Your saved playthrough</h2>
            {savedSession ? (
              <p className="mt-1 break-words text-sm text-ink-muted">
                {savedSession.character_name || "No hero"}
                {savedSession.has_history && savedSession.last_played_at ? ` · last played ${relativeTime(savedSession.last_played_at)}` : " · no story history yet"}
              </p>
            ) : sessions.data === undefined ? (
              <p className="mt-1 text-sm text-ink-muted">Loading saved playthroughs…</p>
            ) : sessions.data.some((session) => session.has_history) ? (
              <p className="mt-1 text-sm text-ink-muted">An older saved story is available below. Its current readiness needs checking before you open it.</p>
            ) : ended ? (
              <p className="mt-1 text-sm text-ink-muted">This campaign ended before a playthrough was started. Reopen it in settings to begin again.</p>
            ) : (
              <p className="mt-1 text-sm text-ink-muted">No saved playthrough yet. Choose a hero and start the adventure.</p>
            )}
          </div>
          <div className="flex flex-wrap gap-2">
            {savedSession && (canContinue || canReadStory) ? (
              <Button onClick={() => { rememberLastSession(savedSession.id); navigate(`/play/${savedSession.id}`); }}>
                {canContinue ? "Continue" : "Read story"}
              </Button>
            ) : null}
            {canStartSession ? (
              <ButtonLink variant={savedSession && (canContinue || canReadStory) ? "secondary" : "primary"} to={`/campaigns/${c.id}/setup`}>
                {savedSession ? "New session" : "Set up first session"}
              </ButtonLink>
            ) : null}
            {!savedSession && !ended && !canStartSession ? <ButtonLink variant="secondary" to={`/campaigns/${c.id}/setup`}>View recovery steps</ButtonLink> : null}
          </div>
        </div>
        <SavedPlaythroughContext session={savedSession} />
        {c.readiness && !c.readiness.play_allowed ? <ReadinessNotice readiness={c.readiness} onRetry={campaign.refetch} className="mt-3" /> : null}
        {c.readiness?.blockers.some((blocker) => blocker.action === "review_adventure") ? (
          <AdventureReview campaign={c} onUpdated={(updated) => campaign.setData(() => updated)} />
        ) : null}
      </section>

      <details className="rounded-card border border-line bg-surface-sunken px-4 py-3">
        <summary className="cursor-pointer text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-400">Campaign settings and administration</summary>
        <div className="mt-4 space-y-4">
          <Field label="Rules edition" hint="Source excerpts from a different edition are excluded. Sources without a declared edition remain unverified.">
            {(p) => <select {...p} className={controlClasses} value={c.rules_edition ?? "2014"}
              onChange={(e) => { void setCampaignRulesEdition(c.id, e.target.value as "2014" | "2024").then((updated) => campaign.setData(() => updated)).catch(setActionError); }}>
              <option value="2014">D&D 5e · 2014</option><option value="2024">D&D 5e · 2024</option>
            </select>}
          </Field>
          <Field label="Progression" hint="Milestones are granted when an encounter is won. Each one is spent on an explicit level-up, up to level 5. Fixed keeps the sheet's level.">
            {(p) => <select {...p} className={controlClasses} value={c.progression_mode ?? "fixed"}
              onChange={(e) => { void setCampaignProgressionMode(c.id, e.target.value as "fixed" | "milestone").then((updated) => campaign.setData(() => updated)).catch(setActionError); }}>
              <option value="fixed">Fixed level</option><option value="milestone">Milestones (level up to 5)</option>
            </select>}
          </Field>
          <Field label="Death" hint="Subdued: a defeat leaves the hero unconscious at 1 HP and the story continues. Death saves: the hero makes death saving throws at 0 HP and can die for good. The mode cannot change while a hero is dying.">
            {(p) => <select {...p} className={controlClasses} value={c.death_mode ?? "subdued"}
              onChange={(e) => { void setCampaignDeathMode(c.id, e.target.value as "subdued" | "death_saves").then((updated) => campaign.setData(() => updated)).catch(setActionError); }}>
              <option value="subdued">Subdued at 1 HP (solo continuation)</option><option value="death_saves">Death saving throws (permanent death)</option>
            </select>}
          </Field>
          <SourceRecovery campaign={c} onUpdated={(updated) => campaign.setData(() => updated)} />
          {c.readiness ? <ReadinessNotice readiness={c.readiness} onRetry={campaign.refetch} /> : null}
          <div className="flex flex-wrap gap-2 border-t border-line pt-3">
            {ended ? (
              <Button size="sm" loading={busy === "reopen"} loadingText="Reopening…" disabled={busy != null} onClick={() => void run("reopen")}>Reopen campaign</Button>
            ) : (
              <Button variant="secondary" size="sm" loading={busy === "end"} loadingText="Ending…" disabled={busy != null} onClick={() => void run("end")}>End campaign</Button>
            )}
            <Button variant="danger" size="sm" loading={busy === "delete"} loadingText="Deleting…" disabled={busy != null} onClick={() => void run("delete")} aria-label="Delete campaign and all its saved playthroughs">Delete campaign</Button>
          </div>
        </div>
      </details>

      <div>
        <Tabs idBase="campaign" label="Campaign sections" items={TABS} value={tab} onChange={selectTab} />
        {/* Re-mount on status change so lists refresh after End/Reopen. */}
        <TabPanel idBase="campaign" id={tab} key={`${tab}:${c.status}`} className="pt-5">
          {tab === "sessions" ? <SessionsTab campaignId={c.id} ended={ended} readiness={c.readiness} playAllowed={c.readiness?.play_allowed === true} /> : null}
          {tab === "characters" ? <CharactersTab campaignId={c.id} progressionMode={c.progression_mode ?? "fixed"} /> : null}
          {tab === "npcs" ? <NpcsTab campaignId={c.id} /> : null}
          {tab === "world" ? <WorldTab campaignId={c.id} /> : null}
        </TabPanel>
      </div>
    </Page>
  );
}
