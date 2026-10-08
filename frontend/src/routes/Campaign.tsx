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
  reopenCampaign,
} from "../api/client";
import { Pill } from "../components/Pill";
import { Button, ButtonLink } from "../components/ui/Button";
import { EmptyState, LoadingText } from "../components/ui/EmptyState";
import { ErrorNotice, OfflineState } from "../components/ui/ErrorNotice";
import { ArrowLeftIcon } from "../components/ui/icons";
import { Page } from "../components/ui/Page";
import { TabPanel, Tabs } from "../components/ui/Tabs";
import { useMode } from "../dev/ModeContext";
import { campaignDisplayName } from "../lib/campaign";
import { dateTime } from "../lib/format";
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
              <h1 className="font-display text-display-md">{name}</h1>
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
          <div className="flex flex-wrap gap-2">
            {ended ? (
              <Button
                size="sm"
                loading={busy === "reopen"}
                loadingText="Reopening…"
                disabled={busy != null}
                onClick={() => void run("reopen")}
              >
                Reopen campaign
              </Button>
            ) : (
              <Button
                variant="secondary"
                size="sm"
                loading={busy === "end"}
                loadingText="Ending…"
                disabled={busy != null}
                onClick={() => void run("end")}
              >
                End campaign
              </Button>
            )}
            <Button
              variant="danger"
              size="sm"
              loading={busy === "delete"}
              loadingText="Deleting…"
              disabled={busy != null}
              onClick={() => void run("delete")}
            >
              Delete
            </Button>
          </div>
        </div>
        <ErrorNotice error={actionError} />
      </header>

      <div>
        <Tabs idBase="campaign" label="Campaign sections" items={TABS} value={tab} onChange={selectTab} />
        {/* Re-mount on status change so lists refresh after End/Reopen. */}
        <TabPanel idBase="campaign" id={tab} key={`${tab}:${c.status}`} className="pt-5">
          {tab === "sessions" ? <SessionsTab campaignId={c.id} ended={ended} /> : null}
          {tab === "characters" ? <CharactersTab campaignId={c.id} /> : null}
          {tab === "npcs" ? <NpcsTab campaignId={c.id} /> : null}
          {tab === "world" ? <WorldTab campaignId={c.id} /> : null}
        </TabPanel>
      </div>
    </Page>
  );
}
