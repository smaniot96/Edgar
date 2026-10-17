import { useRef, useState, type FormEvent } from "react";

import { generateAdventure, uploadAdventure } from "../../api/client";
import type { AdventureGenerateRequest, AdventureRead, AdventureSize } from "../../api/types";
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
  { value: "gigantic", label: "Gigantic", blurb: "An epic with many distinct acts" },
];

import { usePreparationSetup } from "./PreparationFields";

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

/**
 * The identity of one generate attempt. A retry sends the same identity while the request body is
 * unchanged, so an ambiguous failure cannot create a second module. Editing any field starts a new
 * attempt, and so does a successful accept.
 */
type GenerateAttempt = { requestId: string; fingerprint: string };

function GenerateForm({ onCreated }: { onCreated: (a: AdventureRead) => void }) {
  const preparation = usePreparationSetup();
  const [title, setTitle] = useState("");
  const [theme, setTheme] = useState("");
  const [size, setSize] = useState<AdventureSize>("medium");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  // A ref, not state: a double submit before re-render must share the same identity.
  const attemptRef = useRef<GenerateAttempt | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!preparation.setup) return;
    const payload: AdventureGenerateRequest = {
      title: title.trim() || undefined,
      theme: theme.trim() || undefined,
      size,
      preparation: preparation.setup,
    };
    const fingerprint = JSON.stringify(payload);
    const attempt =
      attemptRef.current?.fingerprint === fingerprint
        ? attemptRef.current
        : { requestId: crypto.randomUUID(), fingerprint };
    attemptRef.current = attempt;
    setBusy(true);
    setError(null);
    try {
      const stub = await generateAdventure({ ...payload, request_id: attempt.requestId });
      attemptRef.current = null;
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

      {preparation.fields}
      <ErrorNotice error={error} />
      <Button type="submit" className="min-h-11" disabled={!preparation.setup} loading={busy} loadingText="Starting…">
        Generate campaign
      </Button>
    </form>
  );
}

function UploadForm({ onCreated }: { onCreated: (a: AdventureRead) => void }) {
  const preparation = usePreparationSetup();
  const fileRef = useRef<HTMLInputElement>(null);
  const [rulesEdition, setRulesEdition] = useState<"2014" | "2024" | "unspecified">("2014");
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!file || !preparation.setup) return;
    setBusy(true);
    setError(null);
    try {
      const created = await uploadAdventure(file, title, rulesEdition, preparation.setup);
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
        Already have an adventure module? Pick a PDF. Edgar studies it privately, prepares the
        whole adventure for your hero and checks it before play.
      </p>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
        <Field label="Source rules edition" hint="Use unspecified when the publication does not declare an edition.">
          {(p) => <select {...p} value={rulesEdition} onChange={(e) => setRulesEdition(e.target.value as typeof rulesEdition)} className="rounded-control border border-line bg-surface p-2 text-sm">
            <option value="unspecified">Unspecified</option><option value="2014">2014</option><option value="2024">2024</option>
          </select>}
        </Field>
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
      {preparation.fields}
      <ErrorNotice error={error} />
      <Button type="submit" className="min-h-11" disabled={!file || !preparation.setup} loading={busy} loadingText="Uploading…">
        Upload
      </Button>
    </form>
  );
}
