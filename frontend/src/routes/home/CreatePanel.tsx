import { useRef, useState, type FormEvent } from "react";

import { generateAdventure, uploadAdventure } from "../../api/client";
import type { AdventureRead, AdventureSize } from "../../api/types";
import { Button } from "../../components/ui/Button";
import { ErrorNotice } from "../../components/ui/ErrorNotice";
import { Field, Input, TextField } from "../../components/ui/Field";
import { cardClasses } from "../../components/ui/styles";
import { TabPanel, Tabs } from "../../components/ui/Tabs";
import { cn } from "../../lib/cn";

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

export function CreatePanel({ onCreated }: { onCreated: (a: AdventureRead) => void }) {
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
