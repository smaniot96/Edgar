/**
 * Settings view — Phase 10: user profile
 */

import { useEffect, useState } from "react";

import type { UserRead } from "../api/types";
import { ApiError, apiFetch } from "../api/client";

export default function Settings() {
  const [user, setUser] = useState<UserRead | null>(null);
  const [displayName, setDisplayName] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    apiFetch("/api/users/me")
      .then((r) => r.json() as Promise<UserRead>)
      .then((u) => {
        setUser(u);
        setDisplayName(u.display_name ?? "");
      })
      .catch((e) => setLoadError(e instanceof ApiError ? e.body : String(e)));
  }, []);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      const res = await apiFetch("/api/users/me", {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ display_name: displayName.trim() || null }),
      });
      const u = (await res.json()) as UserRead;
      setUser(u);
      setMsg({ kind: "ok", text: "Saved!" });
    } catch (e) {
      setMsg({ kind: "err", text: e instanceof ApiError ? e.body : String(e) });
    } finally {
      setBusy(false);
    }
  }

  if (loadError) {
    return (
      <div className="mx-auto max-w-lg p-6">
        <h1 className="mb-4 text-xl font-semibold">Settings</h1>
        <p className="text-sm text-red-400">{loadError}</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-lg space-y-6 p-6">
      <h1 className="text-xl font-semibold">Settings</h1>

      <section className="rounded-lg border border-[#2a2c30] bg-[#1a1c20] p-5">
        <h2 className="mb-3 text-sm font-semibold">Profile</h2>
        {user ? (
          <>
            <p className="mb-3 text-xs text-[#9ca3af]">
              Email: <span className="text-[#e6e6e6]">{user.email}</span>
            </p>
            <form onSubmit={(e) => void save(e)} className="space-y-3">
              <div>
                <label className="mb-1 block text-xs text-[#9ca3af]">Display name</label>
                <input
                  type="text"
                  maxLength={255}
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                  className="w-full rounded border border-[#333] bg-[#101216] px-3 py-2 text-sm text-[#e6e6e6] focus:outline-none focus:ring-1 focus:ring-[#3b82f6]"
                />
              </div>
              {msg ? (
                <p className={`text-xs ${msg.kind === "ok" ? "text-green-400" : "text-red-400"}`}>
                  {msg.text}
                </p>
              ) : null}
              <button
                type="submit"
                disabled={busy}
                className="rounded-md bg-[#3b82f6] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
              >
                {busy ? "Saving…" : "Save"}
              </button>
            </form>
          </>
        ) : (
          <p className="text-sm text-[#9ca3af]">Loading…</p>
        )}
      </section>
    </div>
  );
}
