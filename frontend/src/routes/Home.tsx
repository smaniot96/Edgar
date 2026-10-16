/**
 * Campaigns page — the canonical `/campaigns` route (the landing splash lives at `/`).
 *
 * A "campaign" comes from an adventure module: an uploaded PDF or one authored by AI. Modules
 * embed/author in the background; once ready, Play opens the campaign hub (sessions = saves).
 * Returning players see their campaigns first; the create/upload panel sits behind
 * "New campaign" (expanded by default only when there are no campaigns yet).
 */

import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import {
  deleteAdventure,
  getMe,
  getOrCreateCampaign,
  listAdventures,
  listCampaigns,
  listSessions,
} from "../api/client";
import type { AdventureRead } from "../api/types";
import { CampaignCard } from "../components/campaign/CampaignCard";
import { Button } from "../components/ui/Button";
import { EmptyState, LoadingText } from "../components/ui/EmptyState";
import { ErrorNotice, OfflineState } from "../components/ui/ErrorNotice";
import { ChevronIcon, PlayIcon, PlusIcon } from "../components/ui/icons";
import { Page, PageHeader, SectionTitle } from "../components/ui/Page";
import { campaignDisplayName } from "../lib/campaign";
import { buildCards, selectResumeSession, type CardModel } from "../lib/campaignCards";
import { isJobActive } from "../lib/adventureJob";
import { cn } from "../lib/cn";
import { forgetLastSession, readLastSessionId, rememberLastSession } from "../lib/lastSession";
import { useQuery } from "../lib/useQuery";
import { CreatePanel } from "./home/CreatePanel";
import { WelcomeForm } from "./home/WelcomeForm";

const POLL_MS = 3000;

export default function Home() {
  const navigate = useNavigate();
  const me = useQuery("me", (signal) => getMe(signal));
  const user = me.data;
  const ready = user != null;

  const adventures = useQuery(ready ? "adventures" : null, (s) => listAdventures(s));
  const campaigns = useQuery(ready ? "campaigns" : null, (s) => listCampaigns(s));
  const sessions = useQuery(ready ? "sessions" : null, (s) => listSessions(undefined, s));

  const [createOpen, setCreateOpen] = useState<boolean | null>(null);
  const [actionError, setActionError] = useState<unknown>(null);
  const [openingSlug, setOpeningSlug] = useState<string | null>(null);
  const [deletingSlug, setDeletingSlug] = useState<string | null>(null);

  // Poll while anything is still being prepared: one request at a time, timer cleared on
  // unmount or as soon as nothing is pending.
  const hasPending = adventures.data?.some((a) =>
    a.status === "processing" || isJobActive(a.job),
  ) ?? false;
  const { loading: adventuresLoading, refetch: refetchAdventures } = adventures;
  useEffect(() => {
    if (!hasPending || adventuresLoading) return;
    const t = window.setTimeout(refetchAdventures, POLL_MS);
    return () => window.clearTimeout(t);
  }, [hasPending, adventuresLoading, refetchAdventures]);

  // Prefer the newest playable save with history. A recent blocked or empty save must not
  // displace the last story the player can continue reading or playing.
  const lastSessionId = readLastSessionId();
  const rememberedSession =
    lastSessionId != null ? sessions.data?.find((s) => s.id === lastSessionId) : undefined;
  const lastSessionGone = lastSessionId != null && sessions.data !== undefined && !rememberedSession;
  useEffect(() => {
    if (lastSessionGone) forgetLastSession();
  }, [lastSessionGone]);

  const resumeSession = selectResumeSession(sessions.data);

  const cards = useMemo(
    () =>
      user && adventures.data
        ? buildCards(adventures.data, campaigns.data, sessions.data, user.id)
        : [],
    [user, adventures.data, campaigns.data, sessions.data],
  );

  const resumeName = useMemo(() => {
    if (!resumeSession) return null;
    const camp = campaigns.data?.find((c) => c.id === resumeSession.campaign_id);
    if (!camp) return null;
    const adv = adventures.data?.find((a) => camp.adventure_collections.includes(a.slug));
    return campaignDisplayName(camp, adv);
  }, [resumeSession, campaigns.data, adventures.data]);

  async function handlePlay(card: CardModel) {
    setActionError(null);
    if (card.campaign) {
      navigate(`/campaigns/${card.campaign.id}`);
      return;
    }
    setOpeningSlug(card.adventure.slug);
    try {
      const campaign = await getOrCreateCampaign(card.adventure.slug);
      navigate(`/campaigns/${campaign.id}`);
    } catch (err) {
      setActionError(err);
      setOpeningSlug(null);
    }
  }

  async function handleDelete(card: CardModel) {
    if (!window.confirm(`Delete campaign "${card.name}"? This cannot be undone.`)) return;
    setActionError(null);
    setDeletingSlug(card.adventure.slug);
    try {
      await deleteAdventure(card.adventure.slug);
      adventures.refetch();
      campaigns.refetch();
      sessions.refetch();
    } catch (err) {
      setActionError(err);
    } finally {
      setDeletingSlug(null);
    }
  }

  // Shared by Upload and Generate: show the processing card immediately; polling flips it.
  function handleCreated(created: AdventureRead) {
    adventures.setData((prev) => [
      { ...created, status: created.status ?? "processing" },
      ...(prev ?? []).filter((a) => a.slug !== created.slug),
    ]);
    setCreateOpen(false);
  }

  if (me.error && user === undefined) {
    return (
      <Page>
        <OfflineState error={me.error} onRetry={me.refetch} />
      </Page>
    );
  }

  if (user === undefined) {
    return (
      <Page>
        <LoadingText />
      </Page>
    );
  }

  if (user === null) {
    return (
      <Page width="sm">
        <WelcomeForm onCreated={(u) => me.setData(() => u)} />
      </Page>
    );
  }

  const listError = (campaigns.error && campaigns.data === undefined ? campaigns.error : null)
    ?? (adventures.error && adventures.data === undefined ? adventures.error : null);
  const listsLoaded = adventures.data !== undefined && campaigns.data !== undefined;
  const isEmpty = listsLoaded && cards.length === 0;
  const showCreate = createOpen ?? isEmpty;

  return (
    <Page>
      <PageHeader
        title="Campaigns"
        description={`Welcome back, ${user.display_name || user.email}. Each campaign can hold many sessions (saves).`}
        actions={
          <>
            {resumeSession ? (
              <Button
                onClick={() => {
                  rememberLastSession(resumeSession.id);
                  navigate(`/play/${resumeSession.id}`);
                }}
                title={resumeName ? `${resumeSession.readiness?.play_allowed === true ? "Resume" : "Read"} ${resumeName}` : undefined}
              >
                <PlayIcon className="h-3.5 w-3.5" />
                <span className="max-w-[16rem] truncate">
                  {resumeSession.readiness?.play_allowed === true ? "Resume" : "Read story"}{resumeName ? `: ${resumeName}` : " last session"}
                </span>
              </Button>
            ) : null}
            <Button
              variant="secondary"
              aria-expanded={showCreate}
              aria-controls="new-campaign"
              onClick={() => setCreateOpen(!showCreate)}
            >
              <PlusIcon className="h-4 w-4" /> New campaign
              <ChevronIcon
                className={cn("h-4 w-4 transition-transform", showCreate && "rotate-180")}
              />
            </Button>
          </>
        }
      />

      {showCreate ? (
        <section id="new-campaign" aria-labelledby="new-campaign-title" className="space-y-3">
          <SectionTitle>
            <span id="new-campaign-title">New campaign</span>
          </SectionTitle>
          <CreatePanel onCreated={handleCreated} />
        </section>
      ) : null}

      <ErrorNotice error={actionError} />

      <section aria-labelledby="your-campaigns" className="space-y-3">
        <SectionTitle>
          <span id="your-campaigns">Your campaigns</span>
        </SectionTitle>
        {listError ? (
          <ErrorNotice error={listError} onRetry={() => { adventures.refetch(); campaigns.refetch(); }} />
        ) : !listsLoaded ? (
          <LoadingText>Loading campaigns…</LoadingText>
        ) : isEmpty ? (
          <EmptyState title="No campaigns yet">
            Generate one with AI or upload a PDF above — then hit Play.
          </EmptyState>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {cards.map((card) => (
              <CampaignCard
                key={card.adventure.slug}
                card={card}
                onPlay={(c) => void handlePlay(c)}
                onDelete={(c) => void handleDelete(c)}
                opening={openingSlug === card.adventure.slug}
                deleting={deletingSlug === card.adventure.slug}
                onRetried={() => { adventures.refetch(); campaigns.refetch(); }}
              />
            ))}
          </div>
        )}
      </section>
    </Page>
  );
}
