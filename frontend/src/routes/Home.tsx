/**
 * Campaigns page — the canonical `/campaigns` route (the landing splash lives at `/`).
 *
 * A "campaign" comes from an adventure module: an uploaded PDF or one authored by AI. Modules
 * embed/author in the background; once ready, Play opens the campaign hub (sessions = saves).
 * Returning players see their campaigns first; the create/upload panel sits behind
 * "New campaign" (expanded by default only when there are no campaigns yet).
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import {
  createUser,
  deleteAdventure,
  generateAdventure,
  getMe,
  getOrCreateCampaign,
  listAdventures,
  listCampaigns,
  listSessions,
  uploadAdventure,
} from "../api/client";
import type {
  AdventureRead,
  AdventureSize,
  CampaignRead,
  SessionRead,
  UserRead,
} from "../api/types";
import { Pill } from "../components/Pill";
import { Button } from "../components/ui/Button";
import { EmptyState, LoadingText } from "../components/ui/EmptyState";
import { ErrorNotice, OfflineState } from "../components/ui/ErrorNotice";
import { Field, Input, TextField } from "../components/ui/Field";
import { ChevronIcon, PlayIcon, PlusIcon } from "../components/ui/icons";
import { Page, PageHeader, SectionTitle } from "../components/ui/Page";
import { cardClasses } from "../components/ui/styles";
import { TabPanel, Tabs } from "../components/ui/Tabs";
import { useMode } from "../dev/ModeContext";
import { campaignDisplayName } from "../lib/campaign";
import { cn } from "../lib/cn";
import { relativeTime } from "../lib/format";
import { forgetLastSession, readLastSessionId, rememberLastSession } from "../lib/lastSession";
import { useQuery } from "../lib/useQuery";

const POLL_MS = 3000;

// ── First run ───────────────────────────────────────────────────────────────────

function WelcomeForm({ onCreated }: { onCreated: (u: UserRead) => void }) {
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onCreated(await createUser({ email, display_name: displayName.trim() || null }));
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  }

  return (
    <div className={cn(cardClasses, "mx-auto max-w-md space-y-4 p-6")}>
      <h1 className="font-display text-display-md">Welcome to Edgar</h1>
      <p className="text-sm text-ink-muted">
        Edgar is your AI Dungeon Master. First, tell us who you are. Then you can generate a
        campaign with AI or upload your own adventure PDF.
      </p>
      <form onSubmit={(e) => void submit(e)} className="space-y-3">
        <TextField
          label="Email"
          type="email"
          required
          autoComplete="email"
          placeholder="dm@edgar.local"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        <TextField
          label="Display name (optional)"
          autoComplete="nickname"
          placeholder="Your name"
          maxLength={255}
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
        />
        <ErrorNotice error={error} />
        <Button type="submit" className="w-full" loading={busy} loadingText="Creating…">
          Let's go
        </Button>
      </form>
    </div>
  );
}

// ── Create: AI or upload ────────────────────────────────────────────────────────

const SIZES: { value: AdventureSize; label: string; blurb: string }[] = [
  { value: "small", label: "Small", blurb: "A quick one-shot (~1–2h)" },
  { value: "medium", label: "Medium", blurb: "A short adventure, a few scenes" },
  { value: "large", label: "Large", blurb: "A multi-act adventure with subplots" },
  { value: "gigantic", label: "Gigantic", blurb: "An epic, many chapters (takes a few minutes)" },
];

type CreateMode = "ai" | "upload";
const CREATE_TABS = [
  { id: "ai" as const, label: "Generate with AI" },
  { id: "upload" as const, label: "Upload a PDF" },
];

function CreatePanel({ onCreated }: { onCreated: (a: AdventureRead) => void }) {
  const [mode, setMode] = useState<CreateMode>("ai");
  return (
    <div className={cardClasses}>
      <Tabs
        idBase="create"
        label="How to create a campaign"
        items={CREATE_TABS}
        value={mode}
        onChange={setMode}
        stretch
      />
      <TabPanel idBase="create" id={mode} className="p-4">
        {mode === "ai" ? <GenerateForm onCreated={onCreated} /> : <UploadForm onCreated={onCreated} />}
      </TabPanel>
    </div>
  );
}

function GenerateForm({ onCreated }: { onCreated: (a: AdventureRead) => void }) {
  const [title, setTitle] = useState("");
  const [theme, setTheme] = useState("");
  const [size, setSize] = useState<AdventureSize>("medium");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const stub = await generateAdventure({
        title: title.trim() || undefined,
        theme: theme.trim() || undefined,
        size,
      });
      setTitle("");
      setTheme("");
      onCreated({
        slug: stub.slug,
        title: stub.title,
        description: theme.trim() || null,
        level_range: null,
        cover_image: null,
        status: "processing",
      });
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)} className="space-y-4">
      <p className="text-sm text-ink-muted">
        Describe what you want and Edgar will author a brand-new campaign. It works in the
        background — the card shows progress and flips to <strong>Ready</strong> when done.
      </p>
      <div className="flex flex-col gap-3 sm:flex-row">
        <TextField
          fieldClassName="flex-1"
          label="Title (optional)"
          placeholder="The Lighthouse of Whispers"
          maxLength={255}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
        <TextField
          fieldClassName="flex-1"
          label="Theme / premise (optional)"
          placeholder="a haunted lighthouse on a cursed coast"
          maxLength={500}
          value={theme}
          onChange={(e) => setTheme(e.target.value)}
        />
      </div>

      <fieldset>
        <legend className="mb-1.5 text-xs font-medium text-ink-muted">Size</legend>
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          {SIZES.map((s) => (
            <button
              key={s.value}
              type="button"
              aria-pressed={size === s.value}
              onClick={() => setSize(s.value)}
              className={cn(
                "rounded-card border p-3 text-left transition-colors",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-400",
                size === s.value
                  ? "border-ember-500 bg-ember-950/40"
                  : "border-line bg-surface-sunken hover:border-ember-700",
              )}
            >
              <p className="text-sm font-medium">{s.label}</p>
              <p className="mt-0.5 text-xs text-ink-muted">{s.blurb}</p>
            </button>
          ))}
        </div>
      </fieldset>

      <ErrorNotice error={error} />
      <Button type="submit" loading={busy} loadingText="Starting…">
        Generate campaign
      </Button>
    </form>
  );
}

function UploadForm({ onCreated }: { onCreated: (a: AdventureRead) => void }) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const created = await uploadAdventure(file, title);
      setFile(null);
      setTitle("");
      if (fileRef.current) fileRef.current.value = "";
      onCreated(created);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)} className="space-y-4">
      <p className="text-sm text-ink-muted">
        Already have an adventure module? Pick a PDF — it is indexed in the background (this can
        take a few minutes) and becomes playable when ready.
      </p>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
        <Field label="Adventure PDF" required className="flex-1">
          {(p) => (
            <Input
              {...p}
              ref={fileRef}
              type="file"
              accept="application/pdf,.pdf"
              required
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              className="py-1.5 file:mr-3 file:rounded file:border-0 file:bg-surface-raised file:px-3 file:py-1 file:text-xs file:text-ink"
            />
          )}
        </Field>
        <TextField
          fieldClassName="flex-1"
          label="Title (optional)"
          maxLength={255}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
      </div>
      <ErrorNotice error={error} />
      <Button type="submit" disabled={!file} loading={busy} loadingText="Uploading…">
        Upload
      </Button>
    </form>
  );
}

// ── Campaign cards ──────────────────────────────────────────────────────────────

interface CardModel {
  adventure: AdventureRead;
  name: string;
  /** Latest campaign row for this module (active preferred). */
  campaign: CampaignRead | null;
  state: "processing" | "failed" | "ready" | "ended";
  lastPlayed: string | null;
}

function buildCards(
  adventures: AdventureRead[],
  campaigns: CampaignRead[] | undefined,
  sessions: SessionRead[] | undefined,
  userId: number,
): CardModel[] {
  const lastByCampaign = new Map<number, string>();
  for (const s of sessions ?? []) {
    const prev = lastByCampaign.get(s.campaign_id);
    if (!prev || s.started_at > prev) lastByCampaign.set(s.campaign_id, s.started_at);
  }
  const cards = adventures.map((adventure): CardModel => {
    const mine = (campaigns ?? [])
      .filter((c) => c.created_by === userId && c.adventure_collections.includes(adventure.slug))
      .sort((a, b) => b.created_at.localeCompare(a.created_at));
    const campaign = mine.find((c) => c.status === "active") ?? mine[0] ?? null;
    const lastPlayed =
      mine
        .map((c) => lastByCampaign.get(c.id))
        .filter((x): x is string => !!x)
        .sort()
        .at(-1) ?? null;
    const state =
      adventure.status === "processing"
        ? "processing"
        : adventure.status === "failed"
          ? "failed"
          : campaign?.status === "ended"
            ? "ended"
            : "ready";
    const name = campaign
      ? campaignDisplayName(campaign, adventure)
      : adventure.title;
    return { adventure, name, campaign, state, lastPlayed };
  });
  // Most recently played first; never-played keep API order after them.
  return cards
    .map((c, i) => ({ c, i }))
    .sort((a, b) => {
      const la = a.c.lastPlayed ?? "";
      const lb = b.c.lastPlayed ?? "";
      if (la !== lb) return lb.localeCompare(la);
      return a.i - b.i;
    })
    .map(({ c }) => c);
}

function StatusBadge({ state }: { state: CardModel["state"] }) {
  switch (state) {
    case "ready":
      return <Pill tone="success">Ready</Pill>;
    case "ended":
      return <Pill tone="neutral">Ended</Pill>;
    case "failed":
      return <Pill tone="danger">Failed</Pill>;
    default:
      return (
        <Pill tone="info">
          <span aria-hidden className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-info" />
          Processing
        </Pill>
      );
  }
}

function Cover({ adventure, name }: { adventure: AdventureRead; name: string }) {
  if (adventure.cover_image) {
    return (
      <img
        src={`/${adventure.cover_image}`}
        alt=""
        className="h-32 w-full rounded-t-card object-cover"
      />
    );
  }
  const initial = name.replace(/^(the|a|an)\s+/i, "").trim().charAt(0).toUpperCase() || "E";
  return (
    <div
      aria-hidden
      className="relative flex h-32 w-full items-center justify-center overflow-hidden rounded-t-card bg-gradient-to-br from-ember-950/70 via-surface-sunken to-canvas"
    >
      <div
        className="absolute inset-0"
        style={{
          background:
            "radial-gradient(60% 70% at 50% 110%, rgba(255,107,43,0.28), transparent 70%)",
        }}
      />
      <span className="relative font-display text-6xl font-bold text-ember-300/80">{initial}</span>
    </div>
  );
}

function CampaignCard({
  card,
  onPlay,
  onDelete,
  opening,
  deleting,
}: {
  card: CardModel;
  onPlay: (card: CardModel) => void;
  onDelete: (card: CardModel) => void;
  opening: boolean;
  deleting: boolean;
}) {
  const { isDev } = useMode();
  const { adventure, name, state, lastPlayed } = card;
  const playable = state === "ready" || state === "ended";
  const showAdventureTitle = name !== adventure.title;

  return (
    <article className={cn(cardClasses, "flex flex-col")}>
      <Cover adventure={adventure} name={name} />
      <div className="flex flex-1 flex-col p-4">
        <div className="mb-1 flex items-start justify-between gap-2">
          <h3 className="font-display text-display-sm leading-tight">{name}</h3>
          <StatusBadge state={state} />
        </div>
        {showAdventureTitle ? (
          <p className="mb-1 text-xs text-ink-muted">{adventure.title}</p>
        ) : null}
        <p className="mb-2 text-xs text-ink-subtle">
          {lastPlayed ? `Last played ${relativeTime(lastPlayed)}` : "Not played yet"}
        </p>
        {adventure.description ? (
          <p className="mb-2 line-clamp-2 text-sm text-ink-muted">{adventure.description}</p>
        ) : null}
        <div className="mb-3 flex flex-wrap gap-1.5">
          {adventure.level_range ? <Pill>Levels {adventure.level_range}</Pill> : null}
          {isDev ? <Pill className="font-mono">{adventure.slug}</Pill> : null}
          {isDev && adventure.chunks != null ? <Pill>{adventure.chunks} chunks</Pill> : null}
          {isDev && card.campaign ? <Pill className="font-mono">campaign #{card.campaign.id}</Pill> : null}
        </div>
        {state === "failed" ? (
          <p className="mb-3 rounded-control bg-danger-soft px-2 py-1.5 text-xs text-danger">
            Edgar couldn't prepare this campaign. Delete it and try again.
            {isDev && adventure.error ? (
              <span className="mt-1 block font-mono text-[11px] text-ink-muted">{adventure.error}</span>
            ) : null}
          </p>
        ) : null}
        {state === "processing" ? (
          <p className="mb-3 text-xs italic text-ink-muted">
            Building your campaign… larger sizes take a few minutes. You can leave this page; it
            keeps working.
          </p>
        ) : null}
        {state === "ended" ? (
          <p className="mb-3 text-xs text-ink-muted">
            This campaign has ended. Open it to review or reopen it.
          </p>
        ) : null}
        <div className="mt-auto flex gap-2">
          <Button
            className="flex-1"
            size="sm"
            variant={state === "ended" ? "secondary" : "primary"}
            disabled={!playable}
            loading={opening}
            loadingText="Opening…"
            onClick={() => onPlay(card)}
          >
            {state === "ready" ? (
              <>
                <PlayIcon className="h-3.5 w-3.5" /> Play
              </>
            ) : state === "ended" ? (
              "Open"
            ) : state === "failed" ? (
              "Unavailable"
            ) : (
              "Preparing…"
            )}
          </Button>
          <Button
            variant="danger"
            size="sm"
            loading={deleting}
            loadingText="Deleting…"
            onClick={() => onDelete(card)}
            aria-label={`Delete ${name}`}
          >
            Delete
          </Button>
        </div>
      </div>
    </article>
  );
}

// ── Page ────────────────────────────────────────────────────────────────────────

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
  const hasPending = adventures.data?.some((a) => a.status === "processing") ?? false;
  const { loading: adventuresLoading, refetch: refetchAdventures } = adventures;
  useEffect(() => {
    if (!hasPending || adventuresLoading) return;
    const t = window.setTimeout(refetchAdventures, POLL_MS);
    return () => window.clearTimeout(t);
  }, [hasPending, adventuresLoading, refetchAdventures]);

  // "Resume last session" only when that session still exists.
  const lastSessionId = readLastSessionId();
  const lastSession =
    lastSessionId != null ? sessions.data?.find((s) => s.id === lastSessionId) : undefined;
  const lastSessionGone = lastSessionId != null && sessions.data !== undefined && !lastSession;
  useEffect(() => {
    if (lastSessionGone) forgetLastSession();
  }, [lastSessionGone]);

  const cards = useMemo(
    () =>
      user && adventures.data
        ? buildCards(adventures.data, campaigns.data, sessions.data, user.id)
        : [],
    [user, adventures.data, campaigns.data, sessions.data],
  );

  const resumeName = useMemo(() => {
    if (!lastSession) return null;
    const camp = campaigns.data?.find((c) => c.id === lastSession.campaign_id);
    if (!camp) return null;
    const adv = adventures.data?.find((a) => camp.adventure_collections.includes(a.slug));
    return campaignDisplayName(camp, adv);
  }, [lastSession, campaigns.data, adventures.data]);

  async function handlePlay(card: CardModel) {
    setActionError(null);
    if (card.state === "ended" && card.campaign) {
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

  const listError = adventures.error && adventures.data === undefined ? adventures.error : null;
  const isEmpty = adventures.data !== undefined && adventures.data.length === 0;
  const showCreate = createOpen ?? isEmpty;

  return (
    <Page>
      <PageHeader
        title="Campaigns"
        description={`Welcome back, ${user.display_name || user.email}. Each campaign can hold many sessions (saves).`}
        actions={
          <>
            {lastSession ? (
              <Button
                onClick={() => {
                  rememberLastSession(lastSession.id);
                  navigate(`/play/${lastSession.id}`);
                }}
                title={resumeName ? `Resume ${resumeName}` : undefined}
              >
                <PlayIcon className="h-3.5 w-3.5" />
                <span className="max-w-[16rem] truncate">
                  Resume{resumeName ? `: ${resumeName}` : " last session"}
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
          <ErrorNotice error={listError} onRetry={adventures.refetch} />
        ) : adventures.data === undefined ? (
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
              />
            ))}
          </div>
        )}
      </section>
    </Page>
  );
}
