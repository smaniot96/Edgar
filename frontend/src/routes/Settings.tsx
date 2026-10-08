/**
 * Settings — the player's profile (display name). Developer mode lives in the nav toggle.
 */

import { useState } from "react";
import type { FormEvent } from "react";

import { getMe, updateMe } from "../api/client";
import { Button, ButtonLink } from "../components/ui/Button";
import { EmptyState, LoadingText } from "../components/ui/EmptyState";
import { ErrorNotice, OfflineState } from "../components/ui/ErrorNotice";
import { TextField } from "../components/ui/Field";
import { Page, PageHeader } from "../components/ui/Page";
import { cardClasses } from "../components/ui/styles";
import { cn } from "../lib/cn";
import { useQuery } from "../lib/useQuery";

export default function Settings() {
  const me = useQuery("me", (signal) => getMe(signal));
  // `null` = untouched, show the saved value.
  const [draft, setDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const user = me.data;
  const displayName = draft ?? user?.display_name ?? "";

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setSaved(false);
    setError(null);
    try {
      const u = await updateMe({ display_name: displayName.trim() || null });
      me.setData(() => u);
      setDraft(null);
      setSaved(true);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Page width="sm">
      <PageHeader title="Settings" description="Your profile as the Dungeon Master sees it." />

      {me.error && user === undefined ? (
        <OfflineState error={me.error} onRetry={me.refetch} />
      ) : user === undefined ? (
        <LoadingText />
      ) : user === null ? (
        <EmptyState
          title="No profile yet"
          action={<ButtonLink to="/campaigns">Set up your profile</ButtonLink>}
        >
          Create your profile on the Campaigns page first.
        </EmptyState>
      ) : (
        <section aria-labelledby="profile-title" className={cn(cardClasses, "p-5")}>
          <h2 id="profile-title" className="mb-3 text-sm font-semibold">
            Profile
          </h2>
          <p className="mb-4 text-sm text-ink-muted">
            Email: <span className="text-ink">{user.email}</span>
          </p>
          <form onSubmit={(e) => void save(e)} className="space-y-4">
            <TextField
              label="Display name"
              hint="Shown in greetings. Leave empty to use your email."
              maxLength={255}
              value={displayName}
              onChange={(e) => {
                setDraft(e.target.value);
                setSaved(false);
              }}
            />
            <ErrorNotice error={error} />
            <div className="flex items-center gap-3">
              <Button type="submit" loading={busy} loadingText="Saving…">
                Save
              </Button>
              {saved ? (
                <p role="status" className="text-sm text-success">
                  Saved
                </p>
              ) : null}
            </div>
          </form>
        </section>
      )}
    </Page>
  );
}
