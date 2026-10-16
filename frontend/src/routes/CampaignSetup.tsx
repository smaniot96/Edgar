import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import {
  assignCharacterToCampaign,
  createSession,
  getCampaign,
  isNotFound,
  listCharacters,
  listSessions,
} from "../api/client";
import type { CharacterListItem, SessionRead } from "../api/types";
import { CharacterForm } from "../components/CharacterForm";
import { ReadinessNotice } from "../components/campaign/ReadinessNotice";
import { Button, ButtonLink } from "../components/ui/Button";
import { EmptyState, LoadingText } from "../components/ui/EmptyState";
import { ErrorNotice, OfflineState } from "../components/ui/ErrorNotice";
import { ArrowLeftIcon, PlayIcon, PlusIcon } from "../components/ui/icons";
import { Page, PageHeader } from "../components/ui/Page";
import { campaignDisplayName } from "../lib/campaign";
import {
  createRequestId,
  emptyCharacterDraft,
  readCampaignSetupProgress,
  writeCampaignSetupProgress,
  type CampaignSetupProgress,
} from "../lib/campaignSetup";
import { rememberLastSession } from "../lib/lastSession";
import { useQuery } from "../lib/useQuery";

function getSetupSession(sessions: SessionRead[] | undefined, id: number | null) {
  return id == null ? undefined : sessions?.find((session) => session.id === id);
}

export default function CampaignSetup() {
  const { id } = useParams<{ id: string }>();
  const campaignId = Number(id);
  const validId = Number.isInteger(campaignId) && campaignId > 0;
  const navigate = useNavigate();
  const [progress, setProgress] = useState<CampaignSetupProgress>(() =>
    validId ? readCampaignSetupProgress(campaignId) : readCampaignSetupProgress(0),
  );
  const [assigning, setAssigning] = useState(false);
  const [starting, setStarting] = useState(false);
  const [showCreateHero, setShowCreateHero] = useState(() => progress.pendingCharacter != null);
  const [actionError, setActionError] = useState<unknown>(null);
  const [assignmentMessage, setAssignmentMessage] = useState<string | null>(null);
  const [setupMessage, setSetupMessage] = useState<string | null>(null);

  const campaign = useQuery(validId ? `campaign:${campaignId}` : null, (signal) =>
    getCampaign(campaignId, signal),
  );
  const characters = useQuery(validId ? "characters" : null, (signal) => listCharacters(signal));
  const sessions = useQuery(validId ? `sessions:${campaignId}` : null, (signal) =>
    listSessions(campaignId, signal),
  );

  const selectedCharacter = useMemo(
    () => characters.data?.find((character) => character.id === progress.characterId) ?? null,
    [characters.data, progress.characterId],
  );
  const setupSession = getSetupSession(sessions.data, progress.sessionId);
  const canStart = campaign.data?.status === "active" && campaign.data.readiness?.play_allowed === true;
  const readiness = campaign.data?.readiness ?? null;

  function save(next: CampaignSetupProgress) {
    const persisted = writeCampaignSetupProgress(next);
    setProgress(next);
    return persisted;
  }

  function chooseCharacter(character: CharacterListItem | null) {
    setActionError(null);
    setAssignmentMessage(null);
    setSetupMessage(null);
    save({
      ...progress,
      stage: character ? "assignment" : "playthrough",
      characterId: character?.id ?? null,
      assignedCharacterId: character?.current_assignment?.campaign_id === campaignId ? character.id : null,
      sessionId: null,
      pendingSession: null,
    });
  }

  function openSession(session: SessionRead) {
    rememberLastSession(session.id);
    navigate(`/play/${session.id}`);
  }

  async function assignSelected() {
    if (!selectedCharacter || !canStart) return;
    setAssigning(true);
    setActionError(null);
    setAssignmentMessage(null);
    try {
      // The roster is the recovery authority after a lost assignment response.
      const latest = await listCharacters();
      const current = latest.find((character) => character.id === selectedCharacter.id);
      if (current?.current_assignment?.campaign_id === campaignId) {
        save({ ...progress, assignedCharacterId: current.id, stage: "playthrough" });
        return;
      }
      if (current?.current_assignment) {
        setAssignmentMessage(`${current.name} is assigned to “${current.current_assignment.campaign_title}”. Finish or end that campaign before choosing this hero here.`);
        characters.setData(() => latest);
        return;
      }
      await assignCharacterToCampaign(campaignId, selectedCharacter.id);
      save({ ...progress, assignedCharacterId: selectedCharacter.id, stage: "playthrough" });
      characters.setData((previous) => previous?.map((character) => character.id === selectedCharacter.id
        ? { ...character, current_assignment: { campaign_id: campaignId, campaign_title: campaign.data?.title ?? "this campaign" } }
        : character));
    } catch (error) {
      setActionError(error);
      try {
        const latest = await listCharacters();
        const current = latest.find((character) => character.id === selectedCharacter.id);
        characters.setData(() => latest);
        if (current?.current_assignment?.campaign_id === campaignId) {
          save({ ...progress, assignedCharacterId: current.id, stage: "playthrough" });
          setActionError(null);
          setAssignmentMessage("The hero was assigned successfully. Continue to create the saved playthrough.");
        } else if (current?.current_assignment) {
          setAssignmentMessage(`${current.name} is assigned to “${current.current_assignment.campaign_title}”. It was not moved from that campaign.`);
        }
      } catch {
        characters.refetch();
      }
    } finally {
      setAssigning(false);
    }
  }

  async function startSavedPlaythrough() {
    if (!canStart || starting) return;
    // A second tab may have persisted a retry key or completed the save after this tab loaded.
    // Read storage immediately before selecting a request ID so retries share the same intent.
    const latest = readCampaignSetupProgress(campaignId);
    if (latest.sessionId != null) {
      save(latest);
      setSetupMessage("This setup was updated in another tab. Loading its saved playthrough.");
      sessions.refetch();
      return;
    }
    const characterId = progress.characterId;
    if (latest.characterId !== characterId || (characterId != null && latest.assignedCharacterId !== characterId)) {
      save(latest);
      setSetupMessage("This setup changed in another tab. Review the current step before continuing.");
      return;
    }
    if (characterId != null && latest.assignedCharacterId !== characterId) return;
    const pending = latest.pendingSession?.characterId === characterId
      ? latest.pendingSession
      : { requestId: createRequestId(), characterId };
    // Persist the exact intent before the POST. Retries use the same idempotency key and hero.
    if (!save({ ...latest, stage: "playthrough", pendingSession: pending })) {
      setSetupMessage("Browser storage could not retain the retry details. Allow storage and try again. No saved playthrough was created.");
      return;
    }
    setStarting(true);
    setActionError(null);
    setSetupMessage(null);
    try {
      const session = await createSession(campaignId, characterId ?? undefined, pending.requestId);
      const afterRequest = readCampaignSetupProgress(campaignId);
      if (afterRequest.pendingSession?.requestId === pending.requestId) {
        save({ ...afterRequest, stage: "complete", sessionId: session.id, pendingSession: null });
      }
      rememberLastSession(session.id);
      navigate(`/play/${session.id}`);
    } catch (error) {
      setActionError(error);
      sessions.refetch();
    } finally {
      setStarting(false);
    }
  }

  if (!validId) {
    return <Page width="md"><EmptyState title="Campaign not found" action={<ButtonLink to="/campaigns">Back to Campaigns</ButtonLink>}>This setup link is invalid.</EmptyState></Page>;
  }
  if (isNotFound(campaign.error)) {
    return <Page width="md"><EmptyState title="Campaign not found" action={<ButtonLink to="/campaigns">Back to Campaigns</ButtonLink>}>It may have been deleted.</EmptyState></Page>;
  }
  if (campaign.error && campaign.data === undefined) {
    return <Page width="md"><OfflineState error={campaign.error} onRetry={campaign.refetch} /></Page>;
  }
  if (campaign.data === undefined) return <Page width="md"><LoadingText>Loading campaign…</LoadingText></Page>;
  if (campaign.data === null) {
    return <Page width="md"><EmptyState title="Campaign not found" action={<ButtonLink to="/campaigns">Back to Campaigns</ButtonLink>}>It may have been deleted.</EmptyState></Page>;
  }

  const title = campaignDisplayName(campaign.data);
  const alreadyAssigned = selectedCharacter?.current_assignment?.campaign_id === campaignId || progress.assignedCharacterId === progress.characterId;
  const selectedBusyElsewhere = !!selectedCharacter?.current_assignment && selectedCharacter.current_assignment.campaign_id !== campaignId;
  const storedSession = setupSession ?? (progress.sessionId != null ? sessions.data?.find((session) => session.id === progress.sessionId) : undefined);
  const storedSessionReadable = !!storedSession && (storedSession.readiness?.play_allowed === true ||
    (storedSession.has_history && storedSession.readiness?.history_allowed !== false));

  return (
    <Page width="md">
      <PageHeader
        title={`Set up ${title}`}
        description="A campaign is the adventure. A session is one saved playthrough of it."
        actions={<Link to={`/campaigns/${campaignId}`} className="inline-flex items-center gap-1 text-sm text-ink-muted hover:text-ink"><ArrowLeftIcon className="h-3.5 w-3.5" /> Campaign</Link>}
      />

      {!canStart ? (
        readiness ? <ReadinessNotice readiness={readiness} onRetry={campaign.refetch} /> : (
          <div className="rounded-control border border-warning/40 bg-warning-soft p-3 text-sm">
            Campaign readiness could not be checked. Check again before creating a saved playthrough.
            <Button variant="secondary" size="sm" className="ml-2" onClick={campaign.refetch}>Check again</Button>
          </div>
        )
      ) : null}
      <ErrorNotice error={actionError} />
      {setupMessage ? <p role="status" className="mb-4 rounded-control border border-line bg-surface-raised px-3 py-2 text-sm text-ink-muted">{setupMessage}</p> : null}
      {characters.error && characters.data === undefined ? <ErrorNotice error={characters.error} onRetry={characters.refetch} /> : null}
      {sessions.error && sessions.data === undefined ? <ErrorNotice error={sessions.error} onRetry={sessions.refetch} /> : null}

      {storedSession ? (
        <section className="mb-5 rounded-card border border-ember-700/70 bg-surface-raised p-4">
          <h2 className="font-display text-lg">Saved playthrough ready</h2>
          <p className="mt-1 break-words text-sm text-ink-muted">
            {storedSession.character_name || "No hero"} · Session #{storedSession.id}
            {storedSession.has_history && storedSession.last_played_at ? ` · last played ${new Date(storedSession.last_played_at).toLocaleDateString()}` : " · no story history yet"}
          </p>
          {storedSessionReadable ? (
            <div className="mt-3 flex flex-wrap gap-2">
              <Button onClick={() => openSession(storedSession)}><PlayIcon className="h-3.5 w-3.5" /> {storedSession.readiness?.play_allowed === true ? "Continue" : "Read story"}</Button>
              {canStart ? <Button variant="secondary" onClick={() => save({ ...progress, stage: "hero", sessionId: null, pendingSession: null })}>Start another session</Button> : null}
            </div>
          ) : (
            <p className="mt-2 text-sm text-warning">This saved playthrough needs attention. Open the campaign to review its recovery options.</p>
          )}
        </section>
      ) : null}

      {progress.stage !== "complete" && !storedSession ? (
        <ol aria-label="First playthrough setup progress" className="mb-5 grid gap-2 sm:grid-cols-3">
          {["Choose a hero", "Assign the hero", "Create a saved playthrough"].map((label, index) => {
            const done = index === 0 ? progress.characterId != null : index === 1 ? progress.characterId == null || alreadyAssigned : false;
            const current = (progress.stage === "hero" && index === 0) || (progress.stage === "assignment" && index === 1) || (progress.stage === "playthrough" && index === 2);
            return <li key={label} aria-current={current ? "step" : undefined} className={`rounded-control border px-3 py-2 text-sm ${current ? "border-ember-500 bg-ember-950/40 text-ink" : done ? "border-success/40 text-success" : "border-line text-ink-muted"}`}>
              <span className="mr-2 font-mono text-xs">{done ? "✓" : `0${index + 1}`}</span>{label}
            </li>;
          })}
        </ol>
      ) : null}

      {progress.stage === "hero" && !storedSession ? (
        <section className="space-y-4" aria-labelledby="hero-step-title">
          <div>
            <h2 id="hero-step-title" className="font-display text-xl">Choose who will explore</h2>
            <p className="mt-1 text-sm text-ink-muted">Pick a hero you already made, create one, or start without a hero. A busy hero stays with their current campaign.</p>
          </div>
          {characters.error && characters.data === undefined ? <ErrorNotice error={characters.error} onRetry={characters.refetch} /> : characters.data === undefined ? <LoadingText>Loading your heroes…</LoadingText> : null}
          {characters.data?.length ? (
            <div className="grid gap-2 sm:grid-cols-2">
              {characters.data.map((character) => {
                const busyElsewhere = character.current_assignment != null && character.current_assignment.campaign_id !== campaignId;
                return <button key={character.id} type="button" disabled={!canStart || busyElsewhere || !!progress.pendingCharacter}
                  onClick={() => chooseCharacter(character)}
                  className="min-w-0 rounded-card border border-line bg-surface-sunken p-3 text-left hover:border-ember-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-400 disabled:cursor-not-allowed disabled:opacity-60">
                  <span className="block break-words font-medium">{character.name}</span>
                  <span className="mt-1 block text-xs text-ink-muted">Level {character.level} {character.character_class} · HP {character.hp_max}</span>
                  {busyElsewhere ? <span className="mt-1 block break-words text-xs text-warning">Currently adventuring in {character.current_assignment?.campaign_title}</span> : character.current_assignment?.campaign_id === campaignId ? <span className="mt-1 block text-xs text-success">Already assigned here</span> : null}
                </button>;
              })}
            </div>
          ) : characters.data?.length === 0 ? <p className="text-sm text-ink-muted">You have no heroes yet. Create one below or start a playthrough without a hero.</p> : null}
          {showCreateHero ? (
            <CharacterForm
              title="Create a hero"
              submitLabel="Save hero and continue"
              onCancel={() => setShowCreateHero(false)}
              onDraftChange={(characterDraft) => save({ ...progress, characterDraft })}
              initialDraft={progress.characterDraft}
              pendingCreate={progress.pendingCharacter}
              onCreateRequested={(pendingCharacter) => save({ ...progress, pendingCharacter })}
              onEditPendingCreate={() => save({ ...progress, pendingCharacter: null })}
              onCreated={(character) => {
                const next = { ...progress, stage: "assignment" as const, characterId: character.id, assignedCharacterId: null, pendingCharacter: null, characterDraft: emptyCharacterDraft };
                save(next);
                setShowCreateHero(false);
                characters.refetch();
              }}
              className="bg-surface-raised"
            />
          ) : (
            <div className="flex flex-wrap gap-2">
              <Button variant="secondary" disabled={!canStart} onClick={() => setShowCreateHero(true)}><PlusIcon className="h-4 w-4" /> {progress.pendingCharacter ? "Retry saved hero creation" : "Create a hero"}</Button>
              <Button variant="ghost" disabled={!canStart || !!progress.pendingCharacter} onClick={() => chooseCharacter(null)}>Continue without a hero</Button>
            </div>
          )}
        </section>
      ) : null}

      {progress.stage === "assignment" && !storedSession ? (
        <section className="space-y-4" aria-labelledby="assignment-step-title">
          <div>
            <h2 id="assignment-step-title" className="font-display text-xl">Add this hero to the campaign</h2>
            <p className="mt-1 break-words text-sm text-ink-muted">{selectedCharacter ? `${selectedCharacter.name} will be assigned to ${title} before the saved playthrough starts.` : progress.characterId == null ? "Continue without a hero." : "Confirm the selected hero is available before continuing."}</p>
          </div>
          {assignmentMessage ? <p role="status" className="rounded-control border border-warning/40 bg-warning-soft px-3 py-2 text-sm">{assignmentMessage}</p> : null}
          {selectedBusyElsewhere ? <p role="alert" className="rounded-control border border-warning/40 bg-warning-soft px-3 py-2 text-sm">This hero is active in {selectedCharacter?.current_assignment?.campaign_title}. Edgar will not move the hero or change that campaign assignment.</p> : null}
          {progress.characterId != null && !selectedCharacter ? (
            characters.pending || characters.loading ? <LoadingText>Loading the selected hero…</LoadingText> : characters.error ? (
              <p role="status" className="text-sm text-ink-muted">Check your heroes before continuing. The selected hero is still saved in setup.</p>
            ) : (
              <p role="alert" className="rounded-control border border-warning/40 bg-warning-soft px-3 py-2 text-sm">The selected hero is no longer in your hero list. Choose another hero or continue without a hero.</p>
            )
          ) : null}
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" disabled={assigning} onClick={() => save({ ...progress, stage: "hero" })}>Back to heroes</Button>
            {selectedCharacter ? (
              <Button loading={assigning} loadingText="Checking assignment…" disabled={!canStart || selectedBusyElsewhere} onClick={() => void assignSelected()}>
                {alreadyAssigned ? "Continue to saved playthrough" : "Assign hero and continue"}
              </Button>
            ) : progress.characterId == null ? <Button disabled={!canStart} onClick={() => save({ ...progress, stage: "playthrough" })}>Continue</Button> : characters.error ? (
              <Button variant="secondary" onClick={characters.refetch}>Check heroes again</Button>
            ) : !characters.pending && !characters.loading ? <Button variant="secondary" onClick={() => save({ ...progress, stage: "hero", characterId: null, assignedCharacterId: null, pendingSession: null })}>Continue without a hero</Button> : null}
          </div>
        </section>
      ) : null}

      {progress.stage === "playthrough" && !storedSession ? (
        <section className="space-y-4" aria-labelledby="playthrough-step-title">
          <div>
            <h2 id="playthrough-step-title" className="font-display text-xl">Create your saved playthrough</h2>
            <p className="mt-1 text-sm text-ink-muted">{selectedCharacter ? `Starting with ${selectedCharacter.name}.` : "Starting without a hero; the DM will improvise one."} This creates a fresh playthrough. Existing saves stay as they are.</p>
          </div>
          {selectedCharacter && !alreadyAssigned ? <p role="alert" className="text-sm text-warning">Assign this hero before creating the playthrough.</p> : null}
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" disabled={starting || !!progress.pendingSession} onClick={() => save({ ...progress, stage: selectedCharacter ? "assignment" : "hero" })}>Back</Button>
            <Button loading={starting} loadingText="Saving playthrough…" disabled={!canStart || (!!selectedCharacter && !alreadyAssigned)} onClick={() => void startSavedPlaythrough()}>
              <PlayIcon className="h-3.5 w-3.5" /> Start saved playthrough
            </Button>
          </div>
          {progress.pendingSession ? <p role="status" className="text-xs text-ink-subtle">A saved playthrough request is retained. Retry will reuse the same request and hero.</p> : null}
        </section>
      ) : null}

      {progress.stage === "hero" && !canStart && storedSession == null && !characters.error ? (
        <p className="mt-4 text-sm text-ink-muted">Your campaign and existing story are preserved. Resolve the readiness step above before creating a new playthrough.</p>
      ) : null}

      {progress.sessionId != null && sessions.data !== undefined && !storedSession ? (
        <div className="mt-4"><ErrorNotice error={new Error("The saved playthrough could not be found in this campaign. Refresh the session list or return to the campaign; setup progress has been kept.")} onRetry={sessions.refetch} /></div>
      ) : null}

      {progress.sessionId == null && sessions.data?.some((session) => session.has_history && session.readiness?.history_allowed !== false) ? (
        <p className="mt-6 text-xs text-ink-subtle">Older playthroughs remain available in the campaign’s Sessions section.</p>
      ) : null}

      <div className="mt-8"><ButtonLink variant="secondary" to={`/campaigns/${campaignId}`}>Return to campaign</ButtonLink></div>
    </Page>
  );
}
