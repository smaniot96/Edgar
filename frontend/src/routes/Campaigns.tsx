import { useState } from "react";
import { useLocation } from "react-router-dom";

import {
  getMe,
  listAdventures,
  listCampaigns,
  listSessions,
} from "../api/client";
import { CampaignCard } from "../components/campaign/CampaignCard";
import { ButtonLink } from "../components/ui/Button";
import { EmptyState, LoadingText } from "../components/ui/EmptyState";
import { ErrorNotice, OfflineState } from "../components/ui/ErrorNotice";
import { Page, PageHeader } from "../components/ui/Page";
import { selectResumeSession } from "../lib/campaignCards";
import { campaignDisplayName } from "../lib/campaign";
import { rememberLastSession } from "../lib/lastSession";
import { useQuery } from "../lib/useQuery";
import { WelcomeForm } from "./home/WelcomeForm";

export default function Campaigns() {
  const location = useLocation();
  const me = useQuery("me", getMe);
  const campaigns = useQuery(me.data ? "campaigns" : null, listCampaigns);
  const adventures = useQuery(me.data ? "adventures" : null, listAdventures);
  const sessions = useQuery(me.data ? "sessions" : null, (signal) =>
    listSessions(undefined, signal),
  );
  const [showEnded, setShowEnded] = useState(false);
  const notice = (location.state as { notice?: string } | null)?.notice;
  if (me.error && me.data === undefined)
    return (
      <Page>
        <OfflineState error={me.error} onRetry={me.refetch} />
      </Page>
    );
  if (me.data === undefined)
    return (
      <Page>
        <LoadingText />
      </Page>
    );
  if (me.data === null)
    return (
      <Page width="sm">
        <WelcomeForm onCreated={(user) => me.setData(() => user)} />
      </Page>
    );
  // An adventure archive preference must never hide an existing campaign or saved story.
  const visible = (campaigns.data ?? []).filter((campaign) =>
    showEnded ? campaign.status === "ended" : campaign.status === "active",
  );
  const resume = selectResumeSession(
    sessions.data?.filter((session) =>
      campaigns.data?.some(
        (campaign) =>
          campaign.id === session.campaign_id && campaign.status === "active",
      ),
    ),
  );
  const resumeCampaign = campaigns.data?.find(
    (campaign) => campaign.id === resume?.campaign_id,
  );
  const resumeAdventure = adventures.data?.find((adventure) =>
    resumeCampaign?.adventure_collections.includes(adventure.slug),
  );
  const endedCount =
    campaigns.data?.filter((campaign) => campaign.status === "ended").length ??
    0;

  return (
    <Page>
      <PageHeader
        title="Campaigns"
        eyebrow="YOUR STORIES IN PROGRESS"
        description="Your party, your adventure, your story. Each campaign follows one adventure with its own characters and saved sessions."
        actions={<ButtonLink to="/adventures">New campaign</ButtonLink>}
      />
      {notice ? (
        <p
          role="status"
          className="rounded-control border border-line bg-surface-raised p-3 text-sm"
        >
          {notice}
        </p>
      ) : null}
      {resume && resumeCampaign ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-card border border-ember-700/60 bg-surface-sunken p-4">
          <div className="min-w-0">
            <p className="text-xs text-ink-subtle">
              PICK UP WHERE YOU LEFT OFF
            </p>
            <p className="mt-1 break-words font-display text-lg">
              {campaignDisplayName(resumeCampaign, resumeAdventure)}
            </p>
            <p className="text-xs text-ink-muted">
              {resume.character_name || "Your party"}
            </p>
          </div>
          <ButtonLink
            to={`/play/${resume.id}`}
            onClick={() => rememberLastSession(resume.id)}
          >
            {resume.readiness?.play_allowed ? "Resume" : "Read story"}
          </ButtonLink>
        </div>
      ) : null}
      <div
        className="flex flex-wrap items-center gap-2"
        aria-label="Campaign status"
      >
        <button
          type="button"
          aria-pressed={!showEnded}
          onClick={() => setShowEnded(false)}
          className={`min-h-11 rounded-control px-4 text-sm ${!showEnded ? "bg-surface-raised text-ink" : "text-ink-muted"}`}
        >
          Active (
          {campaigns.data?.filter((c) => c.status === "active").length ?? 0})
        </button>
        <button
          type="button"
          aria-pressed={showEnded}
          onClick={() => setShowEnded(true)}
          className={`min-h-11 rounded-control px-4 text-sm ${showEnded ? "bg-surface-raised text-ink" : "text-ink-muted"}`}
        >
          Ended ({endedCount})
        </button>
      </div>
      {adventures.error ? (
        <ErrorNotice error={adventures.error} onRetry={adventures.refetch} />
      ) : null}
      {sessions.error ? (
        <ErrorNotice error={sessions.error} onRetry={sessions.refetch} />
      ) : null}
      {campaigns.error && campaigns.data === undefined ? (
        <OfflineState error={campaigns.error} onRetry={campaigns.refetch} />
      ) : campaigns.data === undefined ? (
        <LoadingText>Loading campaigns…</LoadingText>
      ) : visible.length === 0 ? (
        <EmptyState
          title={showEnded ? "No ended campaigns" : "No active campaigns"}
          action={
            !showEnded ? (
              <ButtonLink to="/adventures">Explore adventures</ButtonLink>
            ) : undefined
          }
        >
          {showEnded
            ? "Completed campaigns will appear here."
            : "Open an adventure and choose one or more characters to begin."}
        </EmptyState>
      ) : (
        <section
          aria-label={showEnded ? "Ended campaigns" : "Active campaigns"}
          className="grid auto-rows-fr gap-5 sm:grid-cols-2 lg:grid-cols-3"
        >
          {visible.map((campaign) => (
            <CampaignCard
              key={campaign.id}
              campaign={campaign}
              adventure={adventures.data?.find((a) =>
                campaign.adventure_collections.includes(a.slug),
              )}
              sessions={sessions.data}
            />
          ))}
        </section>
      )}
    </Page>
  );
}
