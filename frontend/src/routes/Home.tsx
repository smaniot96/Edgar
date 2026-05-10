import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import type { CampaignRead, UserRead } from "../api/types";
import { ApiError, apiFetch } from "../api/client";

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
      setError(err instanceof ApiError ? err.body : String(err));
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-md space-y-4 rounded-lg border border-[#2a2c30] bg-[#1a1c20] p-6">
      <h2 className="text-lg font-semibold">Welcome to Edgar</h2>
      <p className="text-sm text-[#9ca3af]">What's your email? (This identifies you as the DM.)</p>
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

export default function Home() {
  const navigate = useNavigate();
  const [user, setUser] = useState<UserRead | null | "loading">("loading");
  const [campaigns, setCampaigns] = useState<CampaignRead[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);

  const lastSessionId = localStorage.getItem("lastSessionId");

  async function loadUser() {
    try {
      const res = await apiFetch("/api/users/me");
      const u = (await res.json()) as UserRead;
      setUser(u);
      loadCampaigns();
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setUser(null);
      } else {
        setLoadError(err instanceof ApiError ? err.body : String(err));
        setUser(null);
      }
    }
  }

  async function loadCampaigns() {
    try {
      const res = await apiFetch("/api/campaigns");
      const camps = (await res.json()) as CampaignRead[];
      setCampaigns(camps);
    } catch (err) {
      setLoadError(err instanceof ApiError ? err.body : String(err));
    }
  }

  useEffect(() => {
    void loadUser();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  if (user === "loading") {
    return (
      <div className="flex flex-1 items-center justify-center text-sm text-[#9ca3af]">
        Loading…
      </div>
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
    <div className="mx-auto max-w-2xl space-y-6 p-6">
      <div className="flex items-center justify-between">
        <p className="text-sm text-[#9ca3af]">
          Hello, {user.display_name ?? user.email}
        </p>
        {lastSessionId ? (
          <button
            type="button"
            onClick={() => navigate(`/play/${lastSessionId}`)}
            className="rounded-md bg-[#3b82f6] px-3 py-1.5 text-xs font-semibold text-white"
          >
            Resume last session
          </button>
        ) : null}
      </div>

      {loadError ? <p className="text-sm text-red-400">{loadError}</p> : null}

      <section>
        <h2 className="mb-3 text-base font-semibold">Your campaigns</h2>
        {campaigns.length === 0 ? (
          <p className="text-sm italic text-[#555]">
            No campaigns yet — start one below!
          </p>
        ) : (
          <ul className="space-y-2">
            {campaigns.map((c) => (
              <li
                key={c.id}
                className="flex items-center justify-between rounded-lg border border-[#2a2c30] bg-[#1a1c20] px-4 py-3"
              >
                <div>
                  <p className="text-sm font-medium">{c.title}</p>
                  <p className="text-xs text-[#9ca3af]">
                    {c.adventure_collections.join(", ") || "no adventure"} &bull; {c.status}
                  </p>
                </div>
                <Link
                  to={`/campaigns/${c.id}`}
                  className="rounded px-2 py-1 text-xs text-[#60a5fa] hover:underline"
                >
                  Open →
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>

      <Link
        to="/new"
        className="block w-full rounded-md bg-[#3b82f6] py-2.5 text-center text-sm font-semibold text-white hover:bg-[#2563eb]"
      >
        + Start new adventure
      </Link>
    </div>
  );
}
