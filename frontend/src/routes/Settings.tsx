/**
 * Settings — the player's profile (display name) and, for hosted Edgar, the access token.
 * Developer mode lives in the nav toggle.
 */

import { useState } from "react";
import type { FormEvent } from "react";

import { clearApiToken, hasApiToken, setApiToken } from "../api/auth";
import { getMe, updateMe } from "../api/client";
import { Button, ButtonLink } from "../components/ui/Button";
import { EmptyState, LoadingText } from "../components/ui/EmptyState";
import { ErrorNotice, OfflineState } from "../components/ui/ErrorNotice";
import { TextField } from "../components/ui/Field";
import { Page, PageHeader } from "../components/ui/Page";
import { cardClasses } from "../components/ui/styles";
import { cn } from "../lib/cn";
import { useQuery } from "../lib/useQuery";

/**
 * Token entry for hosted (token-mode) Edgar. The value stays in this tab's session storage and is
 * never shown again once saved. Local single-user Edgar needs no token and ignores this card.
 */
function AccessTokenCard({ onChanged }: { onChanged: () => void }) {
  const [present, setPresent] = useState(() => hasApiToken());
  const [value, setValue] = useState("");
  const [notice, setNotice] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  function save(e: FormEvent) {
    e.preventDefault();
    if (!setApiToken(value)) {
      setNotice({ tone: "error", text: "That is not an access token this browser can keep for this tab. Check the token and try again." });
      return;
    }
    setValue("");
    setPresent(true);
    setNotice({ tone: "ok", text: "Access token saved for this tab." });
    onChanged();
  }

  function forget() {
    clearApiToken();
    setPresent(false);
    setNotice({ tone: "ok", text: "Access token removed from this tab." });
    onChanged();
  }

  return (
    <section aria-labelledby="access-title" className={cn(cardClasses, "mb-6 p-5")}>
      <h2 id="access-title" className="mb-3 text-sm font-semibold">
        Access token
      </h2>
      <p className="mb-3 text-sm text-ink-muted">
        Only needed when this Edgar asks for a token (hosted mode). The token stays in this browser tab and
        is cleared when the tab closes. Ask the operator for a token; it cannot be recovered once lost.
      </p>
      <p role="status" className="mb-3 text-sm text-ink">
        {present ? "A token is set for this tab." : "No token is set for this tab."}
      </p>
      <form onSubmit={save} className="space-y-3" autoComplete="off">
        <TextField
          label="Access token"
          type="password"
          autoComplete="off"
          spellCheck={false}
          required={false}
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            setNotice(null);
          }}
        />
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" variant="secondary" disabled={value.trim() === ""}>
            Save token
          </Button>
          {present ? (
            <Button type="button" variant="ghost" onClick={forget}>
              Remove token
            </Button>
          ) : null}
        </div>
      </form>
      {notice ? (
        <p role={notice.tone === "error" ? "alert" : "status"} className={cn("mt-3 text-sm", notice.tone === "error" ? "text-danger" : "text-success")}>
          {notice.text}
        </p>
      ) : null}
    </section>
  );
}

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

      <AccessTokenCard onChanged={me.refetch} />

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
