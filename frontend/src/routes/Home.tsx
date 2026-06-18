/**
 * Campaigns page — the canonical `/campaigns` route (the landing splash lives
 * at `/`).
 *
 * A "Campaign" comes from an adventure module: either an uploaded PDF or one
 * authored by AI. Modules embed/author in the background; once "ready" they can
 * be opened to reach the campaign hub (which holds many sessions / saves).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import type { AdventureRead, AdventureSize, UserRead } from "../api/types";
import {
  ApiError,
  apiFetch,
  deleteAdventure,
  errorMessage,
  generateAdventure,
  getOrCreateCampaign,
  listAdventures,
  uploadAdventure,
} from "../api/client";
import { Pill } from "../components/Pill";

const POLL_MS = 3000;

function WelcomeForm({ onCreated }: { onCreated: () => void }) {
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await apiFetch("/api/users", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ email, display_name: displayName || null }),
      });
      onCreated();
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-md space-y-4 rounded-lg border border-[#2a2c30] bg-[#1a1c20] p-6">
      <h2 className="text-lg font-semibold">Welcome to Edgar</h2>
      <p className="text-sm text-[#9ca3af]">
        Edgar is your AI Dungeon Master. First, tell us who you are — this identifies you as the DM.
        Then you can generate a campaign with AI or upload your own adventure PDF.
      </p>
      <form onSubmit={(e) => void submit(e)} className="space-y-3">
        <div>
          <label className="mb-1 block text-xs text-[#9ca3af]">Email</label>
          <input
            type="email"
            required
            placeholder="dm@edgar.local"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="w-full rounded border border-[#333] bg-[#101216] px-3 py-2 text-sm text-[#e6e6e6] focus:outline-none focus:ring-1 focus:ring-[#3b82f6]"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-[#9ca3af]">Display name (optional)</label>
          <input
            type="text"
            placeholder="Your name"
            maxLength={255}
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            className="w-full rounded border border-[#333] bg-[#101216] px-3 py-2 text-sm text-[#e6e6e6] focus:outline-none focus:ring-1 focus:ring-[#3b82f6]"
          />
        </div>
        {error ? <p className="text-xs text-red-400">{error}</p> : null}
        <button
          type="submit"
          disabled={busy}
          className="w-full rounded-md bg-[#3b82f6] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
        >
          {busy ? "Creating…" : "Let's go"}
        </button>
      </form>
    </div>
  );
}

function StatusBadge({ status }: { status: AdventureRead["status"] }) {
  if (status === "ready" || status == null) {
    return <Pill className="bg-[#10331f] text-green-400">Ready</Pill>;
  }
  if (status === "failed") {
    return <Pill className="bg-[#3a1414] text-red-400">Failed</Pill>;
  }
  return (
    <Pill className="bg-[#1f3a5f] text-[#93c5fd]">
      <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-[#93c5fd]" /> Preparing…
    </Pill>
  );
}

const SIZES: { value: AdventureSize; label: string; blurb: string }[] = [
  { value: "small", label: "Small", blurb: "A quick one-shot (~1–2h)" },
  { value: "medium", label: "Medium", blurb: "A short adventure, a few scenes" },
  { value: "large", label: "Large", blurb: "A multi-act adventure with subplots" },
  {
    value: "gigantic",
    label: "Gigantic",
    blurb: "An epic, many chapters (takes a few minutes)",
  },
];

type CreateMode = "ai" | "upload";

function CreatePanel({ onCreated }: { onCreated: (a: AdventureRead) => void }) {
  const [mode, setMode] = useState<CreateMode>("ai");

  return (
    <div className="rounded-lg border border-[#2a2c30] bg-[#1a1c20]">
      <div className="flex border-b border-[#2a2c30]">
        {(
          [
            { id: "ai" as CreateMode, label: "✨ Generate with AI" },
            { id: "upload" as CreateMode, label: "Upload a PDF" },
          ]
        ).map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setMode(t.id)}
            className={`flex-1 px-4 py-2.5 text-sm font-semibold transition-colors ${
              mode === t.id
                ? "border-b-2 border-[#3b82f6] text-[#e6e6e6]"
                : "border-b-2 border-transparent text-[#9ca3af] hover:text-[#e6e6e6]"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="p-4">
        {mode === "ai" ? (
          <GenerateForm onCreated={onCreated} />
        ) : (
          <UploadForm onCreated={onCreated} />
        )}
      </div>
    </div>
  );
}

function GenerateForm({ onCreated }: { onCreated: (a: AdventureRead) => void }) {
  const [title, setTitle] = useState("");
  const [theme, setTheme] = useState("");
  const [size, setSize] = useState<AdventureSize>("medium");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
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
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)} className="space-y-4">
      <p className="text-xs text-[#9ca3af]">
        Describe what you want and Edgar will author a brand-new campaign for you. It works in the
        background — the card below shows progress and flips to <strong>Ready</strong> when done.
      </p>
      <div className="flex flex-col gap-3 sm:flex-row">
        <div className="flex-1">
          <label className="mb-1 block text-xs text-[#9ca3af]">Title (optional)</label>
          <input
            type="text"
            placeholder="The Lighthouse of Whispers"
            maxLength={255}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className="w-full rounded border border-[#333] bg-[#101216] px-3 py-2 text-sm text-[#e6e6e6] focus:outline-none focus:ring-1 focus:ring-[#3b82f6]"
          />
        </div>
        <div className="flex-1">
          <label className="mb-1 block text-xs text-[#9ca3af]">Theme / premise (optional)</label>
          <input
            type="text"
            placeholder="a haunted lighthouse on a cursed coast"
            maxLength={500}
            value={theme}
            onChange={(e) => setTheme(e.target.value)}
            className="w-full rounded border border-[#333] bg-[#101216] px-3 py-2 text-sm text-[#e6e6e6] focus:outline-none focus:ring-1 focus:ring-[#3b82f6]"
          />
        </div>
      </div>

      <div>
        <label className="mb-1.5 block text-xs text-[#9ca3af]">Size</label>
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          {SIZES.map((s) => (
            <button
              key={s.value}
              type="button"
              onClick={() => setSize(s.value)}
              className={`rounded-lg border p-3 text-left transition-colors ${
                size === s.value
                  ? "border-[#3b82f6] bg-[#1f3a5f]"
                  : "border-[#2a2c30] bg-[#101216] hover:border-[#3b82f6]"
              }`}
            >
              <p className="text-sm font-medium">{s.label}</p>
              <p className="mt-0.5 text-xs text-[#9ca3af]">{s.blurb}</p>
            </button>
          ))}
        </div>
      </div>

      {error ? <p className="text-xs text-red-400">{error}</p> : null}
      <button
        type="submit"
        disabled={busy}
        className="rounded-md bg-[#3b82f6] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
      >
        {busy ? "Starting…" : "✨ Generate campaign"}
      </button>
    </form>
  );
}

function UploadForm({ onCreated }: { onCreated: (a: AdventureRead) => void }) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
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
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)} className="space-y-3">
      <p className="text-xs text-[#9ca3af]">
        Already have an adventure module? Pick a PDF — it embeds in the background (this can take a
        few minutes) and becomes playable when ready.
      </p>
      <div className="flex flex-col gap-3 sm:flex-row">
        <input
          ref={fileRef}
          type="file"
          accept="application/pdf,.pdf"
          required
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          className="flex-1 rounded border border-[#333] bg-[#101216] px-3 py-1.5 text-sm text-[#e6e6e6] file:mr-3 file:rounded file:border-0 file:bg-[#222] file:px-3 file:py-1 file:text-xs file:text-[#e6e6e6] focus:outline-none focus:ring-1 focus:ring-[#3b82f6]"
        />
        <input
          type="text"
          placeholder="Title (optional)"
          maxLength={255}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          className="flex-1 rounded border border-[#333] bg-[#101216] px-3 py-1.5 text-sm text-[#e6e6e6] focus:outline-none focus:ring-1 focus:ring-[#3b82f6]"
        />
        <button
          type="submit"
          disabled={busy || !file}
          className="rounded-md bg-[#3b82f6] px-4 py-1.5 text-sm font-semibold text-white disabled:opacity-50"
        >
          {busy ? "Uploading…" : "Upload"}
        </button>
      </div>
      {error ? <p className="text-xs text-red-400">{error}</p> : null}
    </form>
  );
}

function CampaignCard({
  adventure,
  onOpen,
  onDelete,
  opening,
}: {
  adventure: AdventureRead;
  onOpen: (a: AdventureRead) => void;
  onDelete: (a: AdventureRead) => void;
  opening: boolean;
}) {
  const isReady = adventure.status === "ready" || adventure.status == null;
  const isFailed = adventure.status === "failed";

  return (
    <div className="flex flex-col rounded-lg border border-[#2a2c30] bg-[#1a1c20] p-4">
      {adventure.cover_image ? (
        <img
          src={`/${adventure.cover_image}`}
          alt={adventure.title}
          className="mb-3 w-full rounded object-cover"
          style={{ maxHeight: 140 }}
        />
      ) : null}
      <div className="mb-2 flex items-start justify-between gap-2">
        <p className="text-sm font-semibold">{adventure.title}</p>
        <StatusBadge status={adventure.status} />
      </div>
      {adventure.description ? (
        <p className="mb-2 line-clamp-2 text-xs text-[#9ca3af]">{adventure.description}</p>
      ) : null}
      <div className="mb-3 flex flex-wrap gap-1.5">
        {adventure.level_range ? <Pill>Levels {adventure.level_range}</Pill> : null}
        {isReady && adventure.chunks != null ? <Pill>{adventure.chunks} chunks</Pill> : null}
      </div>
      {isFailed && adventure.error ? (
        <p className="mb-3 rounded bg-[#3a1414] px-2 py-1.5 text-xs text-red-300">
          {adventure.error}
        </p>
      ) : null}
      {!isReady && !isFailed ? (
        <p className="mb-3 text-xs italic text-[#9ca3af]">
          Building your campaign… this can take a few minutes for larger sizes. You can leave this
          page; it will keep working.
        </p>
      ) : null}
      <div className="mt-auto flex gap-2">
        <button
          type="button"
          disabled={!isReady || opening}
          onClick={() => onOpen(adventure)}
          className="flex-1 rounded-md bg-[#3b82f6] px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-40"
        >
          {opening ? "Opening…" : isReady ? "▶ Play" : isFailed ? "Unavailable" : "Preparing…"}
        </button>
        <button
          type="button"
          onClick={() => onDelete(adventure)}
          className="rounded-md border border-red-800 px-3 py-1.5 text-xs text-red-400 hover:bg-red-900"
        >
          Delete
        </button>
      </div>
    </div>
  );
}

export default function Home() {
  const navigate = useNavigate();
  const [user, setUser] = useState<UserRead | null | "loading">("loading");
  const [adventures, setAdventures] = useState<AdventureRead[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [openingSlug, setOpeningSlug] = useState<string | null>(null);

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const lastSessionId =
    typeof localStorage !== "undefined" ? localStorage.getItem("lastSessionId") : null;

  const refreshAdventures = useCallback(async () => {
    try {
      const list = await listAdventures();
      setAdventures(list);
      return list;
    } catch (err) {
      setLoadError(errorMessage(err));
      return null;
    }
  }, []);

  const stopPolling = useCallback(() => {
    if (pollRef.current != null) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const ensurePolling = useCallback(
    (list: AdventureRead[] | null) => {
      const hasPending = (list ?? []).some((a) => a.status === "processing");
      if (hasPending && pollRef.current == null) {
        pollRef.current = setInterval(() => {
          void refreshAdventures().then((next) => {
            const stillPending = (next ?? []).some((a) => a.status === "processing");
            if (!stillPending) stopPolling();
          });
        }, POLL_MS);
      } else if (!hasPending) {
        stopPolling();
      }
    },
    [refreshAdventures, stopPolling],
  );

  const loadUser = useCallback(async () => {
    try {
      const res = await apiFetch("/api/users/me");
      const u = (await res.json()) as UserRead;
      setUser(u);
      const list = await refreshAdventures();
      ensurePolling(list);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setUser(null);
      } else {
        setLoadError(errorMessage(err));
        setUser(null);
      }
    }
  }, [refreshAdventures, ensurePolling]);

  useEffect(() => {
    void loadUser();
    return () => stopPolling();
  }, [loadUser, stopPolling]);

  // Re-evaluate polling whenever the adventure list changes.
  useEffect(() => {
    ensurePolling(adventures);
  }, [adventures, ensurePolling]);

  async function handleOpen(adv: AdventureRead) {
    setActionError(null);
    setOpeningSlug(adv.slug);
    try {
      const campaign = await getOrCreateCampaign(adv.slug);
      navigate(`/campaigns/${campaign.id}`);
    } catch (err) {
      setActionError(errorMessage(err));
      setOpeningSlug(null);
    }
  }

  async function handleDelete(adv: AdventureRead) {
    if (!window.confirm(`Delete campaign "${adv.title}"? This cannot be undone.`)) return;
    setActionError(null);
    try {
      await deleteAdventure(adv.slug);
      await refreshAdventures();
    } catch (err) {
      setActionError(errorMessage(err));
    }
  }

  // Shared by both Upload and Generate: show the processing card immediately,
  // then let the 3s poll flip it to Ready.
  function handleCreated(created: AdventureRead) {
    setAdventures((prev) => {
      const others = (prev ?? []).filter((a) => a.slug !== created.slug);
      return [{ status: "processing", ...created }, ...others];
    });
  }

  if (user === "loading") {
    return (
      <div className="flex flex-1 items-center justify-center text-sm text-[#9ca3af]">Loading…</div>
    );
  }

  if (user === null) {
    return (
      <div className="mx-auto max-w-lg p-6">
        {loadError ? <p className="mb-4 text-sm text-red-400">{loadError}</p> : null}
        <WelcomeForm onCreated={() => void loadUser()} />
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-5xl space-y-6 p-4 sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">Campaigns</h1>
          <p className="text-sm text-[#9ca3af]">
            Hello, {user.display_name ?? user.email}. Generate a campaign with AI or upload a PDF,
            then hit Play. Each campaign can hold many sessions (saves).
          </p>
        </div>
        {lastSessionId ? (
          <button
            type="button"
            onClick={() => navigate(`/play/${lastSessionId}`)}
            className="rounded-md bg-[#3b82f6] px-3 py-1.5 text-xs font-semibold text-white"
          >
            ▶ Resume last session
          </button>
        ) : null}
      </div>

      <CreatePanel onCreated={handleCreated} />

      {loadError ? <p className="text-sm text-red-400">{loadError}</p> : null}
      {actionError ? <p className="text-sm text-red-400">{actionError}</p> : null}

      <section className="space-y-3">
        <h2 className="text-base font-semibold">Your campaigns</h2>
        {adventures === null ? (
          <p className="text-sm text-[#9ca3af]">Loading campaigns…</p>
        ) : adventures.length === 0 ? (
          <div className="rounded-lg border border-dashed border-[#2a2c30] bg-[#15161a] p-8 text-center">
            <p className="text-sm font-medium text-[#e6e6e6]">No campaigns yet</p>
            <p className="mt-1 text-sm text-[#9ca3af]">
              Generate one with AI or upload a PDF above to get started — then click Play.
            </p>
          </div>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {adventures.map((adv) => (
              <CampaignCard
                key={adv.slug}
                adventure={adv}
                onOpen={(a) => void handleOpen(a)}
                onDelete={(a) => void handleDelete(a)}
                opening={openingSlug === adv.slug}
              />
            ))}
          </div>
        )}
      </section>

      <div className="text-center">
        <Link to="/new" className="text-xs text-[#60a5fa] hover:underline">
          Prefer the guided wizard? Start a new adventure →
        </Link>
      </div>
    </div>
  );
}
