import { useState, type FormEvent } from "react";

import { createUser } from "../../api/client";
import type { UserRead } from "../../api/types";
import { Button } from "../../components/ui/Button";
import { ErrorNotice } from "../../components/ui/ErrorNotice";
import { TextField } from "../../components/ui/Field";
import { cardClasses } from "../../components/ui/styles";
import { cn } from "../../lib/cn";

export function WelcomeForm({ onCreated }: { onCreated: (u: UserRead) => void }) {
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
